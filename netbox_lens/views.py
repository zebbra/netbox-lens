import csv
import logging
import operator
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from functools import reduce

from django.apps import apps
from django.conf import settings
from django.contrib import messages
from django.contrib.auth.mixins import PermissionRequiredMixin
from django.db.models import Q
from django.http import HttpResponse
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
from .interface_search import apply_live_status, apply_vm_oper_status, build_interface_list
from .victoria_metrics import fetch_interface_updown_state
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

logger = logging.getLogger(__name__)


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


def _csv_cell(value):
    if value is None:
        return ""
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value


def _csv_response(filename, columns, rows):
    """columns: list of (header, getter) where getter(row) -> cell value."""
    response = HttpResponse(content_type="text/csv")
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    writer = csv.writer(response)
    writer.writerow([header for header, _ in columns])
    for row in rows:
        writer.writerow([_csv_cell(getter(row)) for _, getter in columns])
    return response


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
            r["device_id"] = d.pk
            if r.get("port"):
                r["nb_interface_url"] = iface_map.get((d.pk, r["port"]))


def _apply_active_now(rows, vm_config):
    """Overlay an "active now" (oper=up) flag onto mac-history rows via a
    single bulk VictoriaMetrics query — same source and no-fan-out approach
    as Down-Ports' oper status (see interface_search.apply_vm_oper_status).

    Leaves active_now=None (rendered as "—") when VM has no data for that
    device+port, rather than guessing "down" — a miss can mean the port is
    admin-down, or the device isn't covered by the SNMP module yet.
    """
    if not vm_config or not vm_config.get("url"):
        return
    device_ids = {str(r["device_id"]) for r in rows if r.get("device_id")}
    if not device_ids:
        return
    try:
        status, _ = fetch_interface_updown_state(
            vm_config["url"],
            device_ids,
            timeout=vm_config.get("timeout", 10),
            verify_tls=vm_config.get("verify_ssl", True),
        )
    except Exception:
        return
    for r in rows:
        data = status.get((str(r.get("device_id")), r.get("port")))
        if data:
            r["active_now"] = data["oper"] == "up"


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


def _interpret_rebuild_result(ok, data, error):
    """discobox reports its own failures as HTTP 200 with status="error"/"skipped"
    rather than an HTTP error, so a transport-level success isn't enough here."""
    data = data or {}
    if ok and data.get("status") == "error":
        error = data.get("reason") or (
            "Discobox could not rebuild this device — Netdisco has no record of it "
            "(not discovered yet, or unreachable)."
        )
        ok = False
    elif ok and data.get("status") == "skipped":
        error = f"Rebuild skipped: {data.get('reason') or 'sync is paused or already in progress'}."
        ok = False
    return ok, error


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
            ok, error = _interpret_rebuild_result(ok, data, error)
            context.update({"ok": ok, "data": data, "error": error})

        return render(request, "netbox_lens/rebuild_modal.html", context)


def _run_rebuild_now(config, ip):
    """Runs in a background thread — discobox's /rebuild has no async mode, and
    blocking the request for the ~2-3 minutes a rebuild takes defeats the point
    of a no-preview button. The outcome isn't shown to the user; it's only
    logged, since there's no request left to attach a message to by the time
    this finishes."""
    ok, data, error = rebuild_inventory(config, ip, dry_run=False)
    ok, error = _interpret_rebuild_result(ok, data, error)
    if not ok:
        logger.warning("Rebuild Now failed for %s: %s", ip, error)
    else:
        prune = (data or {}).get("prune") or {}
        deleted = sum(
            prune.get(k) or 0
            for k in ("interfaces_deleted", "modules_deleted", "inventory_deleted", "sfps_deleted")
        )
        logger.info("Rebuild Now completed for %s — %d item(s) deleted.", ip, deleted)


class LensRebuildNowView(PermissionRequiredMixin, View):
    """Applies a rebuild immediately (dry_run=false), skipping the preview modal —
    a plain form post + message banner like Discover/Macsuck/Arpnip, for people
    who don't want to click through a preview first. Fires in a background
    thread and returns instantly rather than blocking on discobox's ~2-3 minute
    synchronous /rebuild call — the outcome isn't known at click time, only
    logged server-side (see _run_rebuild_now)."""
    permission_required = "netbox_lens.trigger_lens"

    def post(self, request, pk):
        device = get_object_or_404(NbDevice, pk=pk)
        ip = _device_ip(device)
        if not ip:
            messages.error(request, "This device has no primary IPv4 address.")
            return redirect(device.get_absolute_url())

        config = settings.PLUGINS_CONFIG.get("netbox_lens", {}).get("discobox", {})
        threading.Thread(target=_run_rebuild_now, args=(config, ip), daemon=True).start()
        messages.info(
            request,
            f"Rebuild request sent for {ip} — this can take a few minutes; "
            "check back on this device's inventory afterward.",
        )
        return redirect(device.get_absolute_url())


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


def _module_rows(result):
    """test_results only covers modules that were actually SNMP-probed — a module
    added unconditionally (mandatory per rule config) never gets tested, so it
    would otherwise vanish from the table despite being part of the final set.
    Returns test_results (tagged tested=True) plus a synthetic, untested row for
    each such module. Trusts a "profile" field set by the modulator itself when
    present; falls back to inferring it from module-set membership for
    modulator versions that don't set it yet."""
    final_fast = set(result.get("final_modules_fast") or [])
    previous_fast = set(result.get("previous_modules_fast") or [])

    def _profile(module_name, given):
        return given or ("fast" if module_name in final_fast or module_name in previous_fast else "normal")

    tested = [
        dict(t, tested=True, profile=_profile(t.get("module"), t.get("profile")))
        for t in (result.get("test_results") or [])
    ]
    tested_names = {t["module"] for t in tested}
    final_all = set(result.get("final_modules") or []) | final_fast
    mandatory = [
        {
            "module": m, "useful": None, "metric_count": None, "duration_seconds": None, "error": None,
            "tested": False, "profile": _profile(m, None),
        }
        for m in sorted(final_all - tested_names)
    ]
    return tested + mandatory


def _annotate_probe_result(result):
    """Add template-friendly derived flags to a ModulationResult dict in place:
    has_fast (whether the fast polling profile applies to this device at all),
    fast_changed (its module set differs from before), the merged module diffs,
    module_rows (test_results plus untested-but-mandatory modules), and pending
    (whether there's anything at all for a follow-up commit to write). Prefers
    fast_profile_enabled/changed_fast straight from the API when the modulator
    sets them, falling back to inference for older modulator versions."""
    has_fast = result.get("fast_profile_enabled")
    if has_fast is None:
        has_fast = bool(
            result.get("previous_modules_fast") or result.get("final_modules_fast") or result.get("resolved_interval_fast")
        )
    fast_changed = result.get("changed_fast")
    if fast_changed is None:
        fast_changed = result.get("previous_modules_fast") != result.get("final_modules_fast")

    result["has_fast"] = has_fast
    result["fast_changed"] = fast_changed
    result["normal_module_diff"] = _module_diff(result.get("previous_modules"), result.get("final_modules"))
    result["fast_module_diff"] = _module_diff(result.get("previous_modules_fast"), result.get("final_modules_fast"))
    result["module_rows"] = _module_rows(result)
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


class LensUpdateModulesView(PermissionRequiredMixin, View):
    """Applies the SNMP module/polling config immediately (dry_run=false,
    wait=false), skipping the preview modal — a plain form post + message
    banner like Discover/Macsuck/Arpnip. wait=false queues it in the
    background and returns instantly, so this is genuinely non-blocking,
    unlike Rebuild Now."""
    permission_required = "netbox_lens.trigger_lens"

    def post(self, request, pk):
        device = get_object_or_404(NbDevice, pk=pk)
        ip = _device_ip(device)
        if not ip:
            messages.error(request, "This device has no primary IPv4 address.")
            return redirect(device.get_absolute_url())

        config = settings.PLUGINS_CONFIG.get("netbox_lens", {}).get("snmp_modulator", {})
        ok, status_code, data, error = snmp_modulator_probe(config, ip, dry_run=False, wait=False)
        data = data or {}
        if not ok:
            messages.error(request, error or "SNMP Modulator request failed.")
        elif status_code == 202 and data.get("status") == "queued":
            messages.success(request, f"Module update queued for {ip}.")
        elif status_code == 202:
            messages.warning(request, f"Module update skipped for {ip}: {data.get('reason', 'already in progress')}.")
        else:
            messages.error(request, f"Unexpected response from SNMP Modulator (HTTP {status_code}).")

        return redirect(device.get_absolute_url())


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
                _apply_active_now(rows, config.get("victoria_metrics", {}))
                if request.GET.get("export") == "csv":
                    return _csv_response(
                        "mac_history.csv",
                        [
                            ("Device", lambda r: r.get("device_name") or r.get("device_ip")),
                            ("Port", lambda r: r.get("port")),
                            ("MAC", lambda r: r.get("mac")),
                            ("VLAN", lambda r: r.get("vlan")),
                            ("Client IP", lambda r: r.get("client_ip")),
                            ("Client Name", lambda r: r.get("client_name")),
                            ("Active Now", lambda r: r.get("active_now")),
                            ("Area", lambda r: r.get("area")),
                            ("First Seen", lambda r: r.get("time_first")),
                            ("Last Seen", lambda r: r.get("time_last")),
                        ],
                        rows,
                    )
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
                if request.GET.get("export") == "csv":
                    return _csv_response(
                        "arp_history.csv",
                        [
                            ("Router", lambda r: r.get("router_name") or r.get("router_ip")),
                            ("MAC", lambda r: r.get("mac")),
                            ("Client IP", lambda r: r.get("client_ip")),
                            ("Client Name", lambda r: r.get("client_name")),
                            ("Last Known", lambda r: r.get("active")),
                            ("Vendor", lambda r: r.get("vendor")),
                            ("Area", lambda r: r.get("area")),
                            ("First Seen", lambda r: r.get("time_first")),
                            ("Last Seen", lambda r: r.get("time_last")),
                        ],
                        rows,
                    )
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
            "locked_oper": self.default_filters.get("oper"),
        }

        if form.is_valid():
            config = settings.PLUGINS_CONFIG.get("netbox_lens", {})
            live = request.GET.get("live") == "1"
            vlan_query = form.cleaned_data.get("vlan") or None
            oper_query = form.cleaned_data.get("oper") or None
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
            vm_meta = {}
            if live:
                backends = get_backends(config)
                rows = apply_live_status(rows, backends, vlan_query=vlan_query)
            else:
                # Single bulk query against VictoriaMetrics's interfaceUpDownState
                # metric — no per-device Netdisco fan-out.
                vm_meta = apply_vm_oper_status(rows, config.get("victoria_metrics", {}))
                # Automatic fallback: only for admin-up rows VM had no data for
                # (either it's down entirely, or these specific devices aren't
                # covered by the if_updown_state module yet) — admin-down rows
                # are expected to have no VM series (dropped at ingest) and
                # don't need a fallback fan-out.
                fallback_rows = [r for r in rows if r.get("admin") == "up" and r.get("oper") is None]
                if fallback_rows:
                    backends = get_backends(config)
                    apply_live_status(fallback_rows, backends, vlan_query=None)
                    vm_meta["fallback_count"] = len(fallback_rows)

            if oper_query:
                rows = [r for r in rows if (r.get("oper") or "").lower() == oper_query]
            total = len(rows)

            if request.GET.get("export") == "csv":
                return _csv_response(
                    "interfaces.csv",
                    [
                        ("Element name", lambda r: r.get("device_name")),
                        ("Interface (ifDescr)", lambda r: r.get("interface_name")),
                        ("Description (ifAlias)", lambda r: r.get("description")),
                        ("VLAN", lambda r: r.get("vlan")),
                        ("Speed", lambda r: r.get("speed")),
                        ("Managed", lambda r: r.get("managed")),
                        ("If admin.", lambda r: r.get("admin")),
                        ("If oper.", lambda r: r.get("oper")),
                        ("Type", lambda r: r.get("type")),
                        ("PoE type", lambda r: r.get("poe_type")),
                        ("Updated", lambda r: r.get("updated")),
                    ],
                    rows,
                )

            context.update({
                "rows": rows,
                "total": total,
                "truncated": truncated,
                "scan_truncated": scan_truncated,
                "searched": True,
                "live": live,
                "vm_meta": vm_meta,
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
                rows, total, truncated, scan_truncated, hidden_count = build_nac_status(
                    backends,
                    device_query=form.cleaned_data.get("device"),
                    interface_query=form.cleaned_data.get("interface") or None,
                    show_disconnected=form.cleaned_data.get("show_disconnected"),
                )
                context.update({
                    "rows": rows,
                    "total": total,
                    "truncated": truncated,
                    "scan_truncated": scan_truncated,
                    "hidden_count": hidden_count,
                    "searched": True,
                })

        return render(request, "netbox_lens/nac_status.html", context)


def _is_switch_or_router(device):
    return bool(device.role) and device.role.slug in ("switch", "router")


if NbDevice:
    @register_model_view(NbDevice, name="lens_macarp", path="lens-mac-arp")
    class DeviceMacArpView(ObjectView):
        """Tab shell only — no Netdisco call here, so opening the tab doesn't
        block on it. device_macarp.html fetches the actual rows via htmx
        from LensDeviceMacArpDataView once the page has already rendered."""
        queryset = NbDevice.objects.all()
        additional_permissions = ["netbox_lens.use_lens"]
        template_name = "netbox_lens/device_macarp.html"
        tab = ViewTab(
            label="IP/MAC Table",
            visible=_is_switch_or_router,
            permission="netbox_lens.use_lens",
        )


class LensDeviceMacArpDataView(PermissionRequiredMixin, View):
    """htmx partial backing DeviceMacArpView's tab — the actual Netdisco
    fetch, deferred so it can't block the tab's own page load."""
    permission_required = "netbox_lens.use_lens"

    def get(self, request, pk):
        device = get_object_or_404(NbDevice, pk=pk)
        ip = _device_ip(device)
        context = {"rows": [], "total": 0, "truncated": False, "lens_device_ip": ip}
        if ip:
            config = settings.PLUGINS_CONFIG.get("netbox_lens", {})
            backends = get_backends(config)
            rows, total, truncated, _ = build_mac_history(backends, device_ip=ip, max_rows=MAX_MACARP_ROWS)
            iface_map = {}
            if NbInterface:
                iface_map = {
                    iface.name: iface.get_absolute_url()
                    for iface in NbInterface.objects.filter(device=device).only("name")
                }
            for r in rows:
                r["device_name"] = device.name
                r["area"] = device.cf.get("service_group")
                if r.get("port"):
                    r["nb_interface_url"] = iface_map.get(r["port"])
            summary = {}
            if backends:
                summary = backends[0].device_summary(ip) or {}
            context.update({
                "rows": rows,
                "total": total,
                "truncated": truncated,
                "lens_last_macsuck": summary.get("last_macsuck"),
                "lens_last_arpnip": summary.get("last_arpnip"),
            })
        return render(request, "netbox_lens/device_macarp_data.html", context)
