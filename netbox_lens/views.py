import operator
from concurrent.futures import ThreadPoolExecutor, as_completed
from functools import reduce

from django.apps import apps
from django.conf import settings
from django.contrib import messages
from django.contrib.auth.mixins import PermissionRequiredMixin
from django.db.models import Q
from django.shortcuts import get_object_or_404, redirect, render
from django.views import View
from netbox.views.generic import ObjectView
from utilities.htmx import htmx_partial
from utilities.views import ViewTab, register_model_view

from .arp_history import build_arp_history
from .backends import get_backends
from .discobox import health as discobox_health
from .discobox import rebuild_inventory, set_paused, stats as discobox_stats, sync_device
from .forms import ArpHistoryForm, InterfaceSearchForm, MacHistoryForm, NacStatusForm, NodeSearchForm
from .interface_search import apply_live_status, build_interface_list
from .mac_history import build_mac_history
from .nac_status import build_nac_status
from .snmp_modulator import health as snmp_modulator_health
from .snmp_modulator import stats as snmp_modulator_stats
from .snmp_modulator import probe as snmp_modulator_probe

try:
    from dcim.models import Device as NbDevice
    from dcim.models import Interface as NbInterface
except ImportError:
    NbDevice = None
    NbInterface = None

MAX_MACARP_ROWS = 500


def _device_ip(device):
    if not device.primary_ip4:
        return None
    return str(device.primary_ip4.address.ip)


def _enrich_results(results):
    """Attach nb_device_url to sighting/ip dicts where a matching NetBox device exists."""
    if not NbDevice:
        return
    names = set()
    for r in results or []:
        for s in r.sightings or []:
            name = (s.get("device") or {}).get("name") or s.get("switch")
            if name:
                names.add(name)
        for ip in r.ips or []:
            name = ip.get("router_name")
            if name:
                names.add(name)
        for mac in r.macs or []:
            name = mac.get("router_name")
            if name:
                names.add(name)
    if names:
        url_map = {d.name: d.get_absolute_url() for d in NbDevice.objects.filter(name__in=names)}
        for r in results or []:
            for s in r.sightings or []:
                name = (s.get("device") or {}).get("name") or s.get("switch")
                if name and name in url_map:
                    s["nb_device_url"] = url_map[name]
            for ip in r.ips or []:
                name = ip.get("router_name")
                if name and name in url_map:
                    ip["nb_device_url"] = url_map[name]
            for mac in r.macs or []:
                name = mac.get("router_name")
                if name and name in url_map:
                    mac["nb_device_url"] = url_map[name]

    ips = {d["ip"] for r in results or [] for d in (r.devices or []) if d.get("ip")}
    if ips:
        q = reduce(operator.or_, (Q(primary_ip4__address__net_host=ip) for ip in ips))
        ip_url_map = {
            str(d.primary_ip4.address.ip): d.get_absolute_url()
            for d in NbDevice.objects.filter(q).select_related("primary_ip4")
        }
        for r in results or []:
            for d in r.devices or []:
                if d.get("ip") in ip_url_map:
                    d["nb_device_url"] = ip_url_map[d["ip"]]


def _enrich_mac_history(rows):
    """Attach nb_device_url and area (service_group) to mac-history rows by resolving
    each distinct device IP to its NetBox Device."""
    if not NbDevice:
        return
    ips = {r["device_ip"] for r in rows if r.get("device_ip")}
    if not ips:
        return
    q = reduce(operator.or_, (Q(primary_ip4__address__net_host=ip) for ip in ips))
    device_map = {
        str(d.primary_ip4.address.ip): d
        for d in NbDevice.objects.filter(q).select_related("primary_ip4")
    }
    iface_map = {}
    if NbInterface and device_map:
        for iface in NbInterface.objects.filter(device__in=device_map.values()).only("device_id", "name"):
            iface_map[(iface.device_id, iface.name)] = iface.get_absolute_url()
    for r in rows:
        d = device_map.get(r.get("device_ip"))
        if d:
            r["nb_device_url"] = d.get_absolute_url()
            r["area"] = d.cf.get("service_group")
            r["device_name"] = r.get("device_name") or d.name
            if r.get("port"):
                r["nb_interface_url"] = iface_map.get((d.pk, r["port"]))


def _enrich_arp_history(rows):
    """Attach nb_device_url and area (service_group) to arp-history rows by resolving
    each distinct router IP to its NetBox Device."""
    if not NbDevice:
        return
    ips = {r["router_ip"] for r in rows if r.get("router_ip")}
    if not ips:
        return
    q = reduce(operator.or_, (Q(primary_ip4__address__net_host=ip) for ip in ips))
    device_map = {
        str(d.primary_ip4.address.ip): d
        for d in NbDevice.objects.filter(q).select_related("primary_ip4")
    }
    for r in rows:
        d = device_map.get(r.get("router_ip"))
        if d:
            r["nb_device_url"] = d.get_absolute_url()
            r["area"] = d.cf.get("service_group")


class LensStatusView(PermissionRequiredMixin, View):
    permission_required = "netbox_lens.use_lens"

    def get(self, request):
        config = settings.PLUGINS_CONFIG.get("netbox_lens", {})
        backends = get_backends(config)
        discobox_config = config.get("discobox", {})
        modulator_config = config.get("snmp_modulator", {})

        statuses = []
        with ThreadPoolExecutor() as executor:
            futures = {executor.submit(b.status): b for b in backends}
            futures[executor.submit(discobox_health, discobox_config)] = "discobox"
            futures[executor.submit(snmp_modulator_health, modulator_config)] = "snmp_modulator"

            discobox_result = None
            modulator_result = None
            for future in as_completed(futures):
                tag = futures[future]
                if tag == "discobox":
                    ok, data, error = future.result()
                    discobox_result = {"ok": ok, "data": data, "error": error}
                elif tag == "snmp_modulator":
                    ok, data, error = future.result()
                    modulator_result = {"ok": ok, "data": data, "error": error}
                else:
                    statuses.append(future.result())

        # Only fetch the richer /stats snapshot for services whose /health
        # just succeeded — no point waiting on stats from something already
        # confirmed unreachable.
        with ThreadPoolExecutor() as executor:
            stats_futures = {}
            if discobox_result and discobox_result["ok"]:
                stats_futures[executor.submit(discobox_stats, discobox_config)] = "discobox"
            if modulator_result and modulator_result["ok"]:
                stats_futures[executor.submit(snmp_modulator_stats, modulator_config)] = "snmp_modulator"
            for future in as_completed(stats_futures):
                tag = stats_futures[future]
                ok, data, error = future.result()
                result = {"ok": ok, "data": data, "error": error}
                if tag == "discobox":
                    discobox_result["stats"] = result
                else:
                    modulator_result["stats"] = result

        return render(request, "netbox_lens/status.html", {
            "statuses": statuses,
            "discobox_health": discobox_result,
            "snmp_modulator_health": modulator_result,
            "lens_can_trigger": request.user.has_perm("netbox_lens.trigger_lens"),
            "lens_version": apps.get_app_config("netbox_lens").version,
            "config_error": None if backends else (
                "No backends configured. Add at least one backend to "
                "PLUGINS_CONFIG['netbox_lens']['backends']."
            ),
        })


class LensDiscoboxPauseView(PermissionRequiredMixin, View):
    permission_required = "netbox_lens.trigger_lens"

    def post(self, request):
        paused = request.POST.get("paused") == "true"
        config = settings.PLUGINS_CONFIG.get("netbox_lens", {}).get("discobox", {})
        ok, data, error = set_paused(config, paused=paused)
        if not ok:
            messages.error(request, error)
        else:
            action = "paused" if paused else "resumed"
            queued = (data or {}).get("queued", 0)
            messages.success(request, f"Discobox sync {action} ({queued} queued).")
        return redirect("plugins:netbox_lens:status")


class LensSearchView(PermissionRequiredMixin, View):
    permission_required = "netbox_lens.use_lens"

    def get(self, request):
        form = NodeSearchForm(request.GET or None)
        context = {"form": form}

        if form.is_valid():
            query = form.cleaned_data["q"]
            partial = form.cleaned_data.get("partial", False)
            date_from = form.cleaned_data.get("date_from")
            date_to = form.cleaned_data.get("date_to")
            # Partial (wildcard) matches without an explicit date range stay
            # active-only — combining partial with a full archived scan is
            # expensive on Netdisco's side for broad queries. But if the user
            # explicitly picked a range, honor it even in partial mode.
            since = date_from.isoformat() if date_from else None
            until = date_to.isoformat() if date_to else None
            archived = bool(since)

            config = settings.PLUGINS_CONFIG.get("netbox_lens", {})
            backends = get_backends(config)

            if not backends:
                context["config_error"] = (
                    "No backends are configured. Add at least one backend to "
                    "PLUGINS_CONFIG['netbox_lens']['backends']."
                )
            else:
                results = [None] * len(backends)
                with ThreadPoolExecutor() as executor:
                    futures = {
                        executor.submit(b.search, query, partial, archived, since, until): i
                        for i, b in enumerate(backends)
                    }
                    for future in as_completed(futures):
                        results[futures[future]] = future.result()

                _enrich_results(results)
                context["results"] = results
                context["query"] = query

        if htmx_partial(request):
            return render(request, "netbox_lens/search_results.html", context)

        return render(request, "netbox_lens/search.html", context)


class LensTriggerJobView(PermissionRequiredMixin, View):
    permission_required = "netbox_lens.trigger_lens"
    job_method = "trigger_discover"

    def post(self, request, pk):
        device = get_object_or_404(NbDevice, pk=pk)
        ip = _device_ip(device)
        if not ip:
            messages.error(request, "This device has no primary IPv4 address.")
            return redirect(device.get_absolute_url())

        config = settings.PLUGINS_CONFIG.get("netbox_lens", {})
        backends = get_backends(config)
        if not backends:
            messages.error(request, "No backends are configured.")
            return redirect(device.get_absolute_url())

        kwargs = {}
        if self.job_method == "trigger_discover":
            auth_profile = device.cf.get("snmp_auth_profile")
            if auth_profile:
                kwargs["auth_profile"] = auth_profile

        for backend in backends:
            success, message = getattr(backend, self.job_method)(ip, **kwargs)
            if success:
                messages.success(request, message)
            else:
                messages.error(request, message)

        return redirect(device.get_absolute_url())


class LensRebuildInventoryView(PermissionRequiredMixin, View):
    permission_required = "netbox_lens.trigger_lens"

    def post(self, request, pk):
        device = get_object_or_404(NbDevice, pk=pk)
        ip = _device_ip(device)
        dry_run = request.POST.get("dry_run", "true") != "false"
        context = {"device": device, "dry_run": dry_run}

        if not ip:
            context["error"] = "This device has no primary IPv4 address."
        else:
            config = settings.PLUGINS_CONFIG.get("netbox_lens", {}).get("discobox", {})
            ok, data, error = rebuild_inventory(config, ip, dry_run=dry_run)
            # discobox reports its own failures as HTTP 200 with status="error"/"skipped"
            # rather than an HTTP error, so a transport-level success isn't enough here.
            if ok and data.get("status") == "error":
                error = data.get("reason") or (
                    "Discobox could not rebuild this device — Netdisco has no record of it "
                    "(not discovered yet, or unreachable)."
                )
                ok = False
            elif ok and data.get("status") == "skipped":
                error = f"Rebuild skipped: {data.get('reason') or 'sync is paused or already in progress'}."
                ok = False
            context.update({"ok": ok, "data": data, "error": error})

        return render(request, "netbox_lens/rebuild_modal.html", context)


class LensSyncView(PermissionRequiredMixin, View):
    permission_required = "netbox_lens.trigger_lens"

    def post(self, request, pk):
        device = get_object_or_404(NbDevice, pk=pk)
        ip = _device_ip(device)
        if not ip:
            messages.error(request, "This device has no primary IPv4 address.")
            return redirect(device.get_absolute_url())

        force = request.POST.get("force") == "true"
        if force and not request.user.is_superuser:
            messages.error(request, "Force sync is restricted to administrators.")
            return redirect(device.get_absolute_url())

        config = settings.PLUGINS_CONFIG.get("netbox_lens", {}).get("discobox", {})
        ok, data, error = sync_device(config, ip, force=force)
        if not ok:
            messages.error(request, error)
        elif (data or {}).get("status") == "queued":
            messages.success(request, f"Sync from Netdisco to NetBox queued for {ip}.")
        else:
            reason = (data or {}).get("reason") or "unknown reason"
            messages.warning(request, f"Sync skipped for {ip}: {reason}.")

        return redirect(device.get_absolute_url())


def _module_diff(previous, final):
    """Merge a before/after module list into one sorted list tagged with
    each module's status, so the template can render a single list with
    additions/removals highlighted instead of two separate before/after lists."""
    previous = set(previous or [])
    final = set(final or [])
    return [
        {
            "name": m,
            "status": "added" if m in final and m not in previous else
                      "removed" if m in previous and m not in final else "unchanged",
        }
        for m in sorted(previous | final)
    ]


def _annotate_probe_result(result):
    """Add template-friendly derived flags to a ModulationResult dict in place:
    has_fast (whether the fast polling profile applies to this device at all),
    fast_changed (its module set differs from before), the merged module diffs,
    and pending (whether there's anything at all for a follow-up commit to write)."""
    fast_changed = result.get("previous_modules_fast") != result.get("final_modules_fast")
    result["has_fast"] = bool(
        result.get("previous_modules_fast") or result.get("final_modules_fast") or result.get("resolved_interval_fast")
    )
    result["fast_changed"] = fast_changed
    result["normal_module_diff"] = _module_diff(result.get("previous_modules"), result.get("final_modules"))
    result["fast_module_diff"] = _module_diff(result.get("previous_modules_fast"), result.get("final_modules_fast"))
    result["pending"] = any([
        result.get("changed"),
        result.get("auth_changed"),
        result.get("polling_interval"),
        result.get("polling_timeout"),
        result.get("polling_interval_fast"),
        result.get("polling_timeout_fast"),
        result.get("pending_add_tags"),
        result.get("pending_remove_tags"),
        fast_changed,
    ])


class LensProbeView(PermissionRequiredMixin, View):
    permission_required = "netbox_lens.trigger_lens"

    def post(self, request, pk):
        device = get_object_or_404(NbDevice, pk=pk)
        ip = _device_ip(device)
        dry_run = request.POST.get("dry_run", "true") != "false"
        wait = request.POST.get("wait", "true") != "false"
        context = {"device": device, "dry_run": dry_run, "wait": wait}

        if not ip:
            context["error"] = "This device has no primary IPv4 address."
        else:
            config = settings.PLUGINS_CONFIG.get("netbox_lens", {}).get("snmp_modulator", {})
            ok, status_code, data, error = snmp_modulator_probe(config, ip, dry_run=dry_run, wait=wait)
            if ok and status_code == 200 and isinstance(data, dict) and isinstance(data.get("result"), dict):
                _annotate_probe_result(data["result"])
            context.update({"ok": ok, "status_code": status_code, "data": data, "error": error})

        return render(request, "netbox_lens/probe_modal.html", context)


class LensMacHistoryView(PermissionRequiredMixin, View):
    permission_required = "netbox_lens.use_lens"

    def get(self, request):
        form = MacHistoryForm(request.GET or None)
        context = {"form": form}

        if form.is_valid():
            config = settings.PLUGINS_CONFIG.get("netbox_lens", {})
            backends = get_backends(config)
            if not backends:
                context["config_error"] = (
                    "No backends are configured. Add at least one backend to "
                    "PLUGINS_CONFIG['netbox_lens']['backends']."
                )
            else:
                rows, total, truncated, port_truncated = build_mac_history(
                    backends,
                    device_query=form.cleaned_data.get("device") or None,
                    interface_query=form.cleaned_data.get("interface") or None,
                    vlan_query=form.cleaned_data.get("vlan") or None,
                    mac_query=form.cleaned_data.get("mac") or None,
                    client_query=form.cleaned_data.get("client") or None,
                    date_from=form.cleaned_data.get("date_from"),
                    date_to=form.cleaned_data.get("date_to"),
                )
                _enrich_mac_history(rows)
                context.update({
                    "rows": rows,
                    "total": total,
                    "truncated": truncated,
                    "port_truncated": port_truncated,
                    "searched": True,
                })

        return render(request, "netbox_lens/mac_history.html", context)


class LensArpHistoryView(PermissionRequiredMixin, View):
    permission_required = "netbox_lens.use_lens"

    def get(self, request):
        form = ArpHistoryForm(request.GET or None)
        context = {"form": form}

        if form.is_valid():
            config = settings.PLUGINS_CONFIG.get("netbox_lens", {})
            backends = get_backends(config)
            if not backends:
                context["config_error"] = (
                    "No backends are configured. Add at least one backend to "
                    "PLUGINS_CONFIG['netbox_lens']['backends']."
                )
            else:
                rows, total, truncated = build_arp_history(
                    backends,
                    mac_query=form.cleaned_data.get("mac") or None,
                    client_query=form.cleaned_data.get("client") or None,
                    device_query=form.cleaned_data.get("device") or None,
                    date_from=form.cleaned_data.get("date_from"),
                    date_to=form.cleaned_data.get("date_to"),
                )
                _enrich_arp_history(rows)
                context.update({
                    "rows": rows,
                    "total": total,
                    "truncated": truncated,
                    "searched": True,
                })

        return render(request, "netbox_lens/arp_history.html", context)


class LensInterfaceSearchView(PermissionRequiredMixin, View):
    permission_required = "netbox_lens.use_lens"
    page_title = "Default"
    default_filters = {}

    def get(self, request):
        data = request.GET.copy()
        for key, value in self.default_filters.items():
            data.setdefault(key, value)
        form = InterfaceSearchForm(data or None)
        context = {
            "form": form,
            "page_title": self.page_title,
            "locked_admin": self.default_filters.get("admin"),
        }

        if form.is_valid():
            config = settings.PLUGINS_CONFIG.get("netbox_lens", {})
            live = request.GET.get("live") == "1"
            vlan_query = form.cleaned_data.get("vlan") or None
            rows, total, truncated, scan_truncated = build_interface_list(
                device_query=form.cleaned_data.get("device") or None,
                interface_query=form.cleaned_data.get("interface") or None,
                description_query=form.cleaned_data.get("description") or None,
                # Deferred to apply_live_status() below when live — NetBox rarely
                # has VLAN set, so filtering on it now would zero out every row
                # before the live refresh has a chance to populate real values.
                vlan_query=None if live else vlan_query,
                speed_query=form.cleaned_data.get("speed") or None,
                managed_query=form.cleaned_data.get("managed") or None,
                admin_query=form.cleaned_data.get("admin") or None,
                grafana_template=config.get("grafana_interface_url"),
            )
            if live:
                backends = get_backends(config)
                rows = apply_live_status(rows, backends, vlan_query=vlan_query)
                total = len(rows)

            context.update({
                "rows": rows,
                "total": total,
                "truncated": truncated,
                "scan_truncated": scan_truncated,
                "searched": True,
                "live": live,
            })

        return render(request, "netbox_lens/interface_search.html", context)


class LensNacStatusView(PermissionRequiredMixin, View):
    permission_required = "netbox_lens.use_lens"

    def get(self, request):
        form = NacStatusForm(request.GET or None)
        context = {"form": form}

        if form.is_valid():
            config = settings.PLUGINS_CONFIG.get("netbox_lens", {})
            backends = get_backends(config)
            if not backends:
                context["config_error"] = (
                    "No backends are configured. Add at least one backend to "
                    "PLUGINS_CONFIG['netbox_lens']['backends']."
                )
            else:
                rows, total, truncated, scan_truncated = build_nac_status(
                    backends,
                    device_query=form.cleaned_data.get("device"),
                    interface_query=form.cleaned_data.get("interface") or None,
                )
                context.update({
                    "rows": rows,
                    "total": total,
                    "truncated": truncated,
                    "scan_truncated": scan_truncated,
                    "searched": True,
                })

        return render(request, "netbox_lens/nac_status.html", context)


if NbDevice:
    @register_model_view(NbDevice, name="lens_macarp", path="lens-mac-arp")
    class DeviceMacArpView(ObjectView):
        queryset = NbDevice.objects.all()
        additional_permissions = ["netbox_lens.use_lens"]
        template_name = "netbox_lens/device_macarp.html"
        tab = ViewTab(
            label="MAC",
            permission="netbox_lens.use_lens",
        )

        def get_extra_context(self, request, instance):
            ip = _device_ip(instance)
            if not ip:
                return {"rows": [], "lens_device_ip": None, "total": 0, "truncated": False}

            config = settings.PLUGINS_CONFIG.get("netbox_lens", {})
            backends = get_backends(config)
            rows, total, truncated, _ = build_mac_history(backends, device_ip=ip, max_rows=MAX_MACARP_ROWS)
            iface_map = {}
            if NbInterface:
                iface_map = {
                    iface.name: iface.get_absolute_url()
                    for iface in NbInterface.objects.filter(device=instance).only("name")
                }
            for r in rows:
                r["device_name"] = instance.name
                r["area"] = instance.cf.get("service_group")
                if r.get("port"):
                    r["nb_interface_url"] = iface_map.get(r["port"])
            return {
                "rows": rows,
                "lens_device_ip": ip,
                "total": total,
                "truncated": truncated,
            }
