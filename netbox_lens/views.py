import csv
import logging
import operator
import re
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, timedelta
from functools import reduce
from itertools import groupby

from django.apps import apps
from django.conf import settings
from django.contrib import messages
from django.contrib.auth.mixins import PermissionRequiredMixin
from django.db.models import F, Q
from django.http import HttpResponse, QueryDict
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.translation import gettext as _
from django.views import View
from netbox.views.generic import ObjectView
from utilities.htmx import htmx_partial
from utilities.views import ViewTab, register_model_view

from .arp_history import build_arp_history
from .backends import get_backends
from .backends.base import SearchResult
from .discobox import health as discobox_health
from .discobox import rebuild_inventory, refresh_ip, set_paused, sync_all, stats as discobox_stats, sync_device
from .down_ports import build_down_ports
from .forms import BASIS_CHOICES, ArpHistoryForm, DownPortsForm, InterfaceSearchForm, MacHistoryForm, NacStatusForm, NodeSearchForm
from .interface_search import apply_live_status, apply_vm_oper_status, build_interface_list
from .template_content import apply_ap_radio_links, apply_transit_ports, device_panel_context, device_panel_enabled, interface_nodes_context, interface_port_context
from .mac_history import build_mac_history
from .nac_status import build_nac_status
from .snmp_modulator import health as snmp_modulator_health
from .snmp_modulator import stats as snmp_modulator_stats
from .snmp_modulator import probe as snmp_modulator_probe

try:
    from dcim.models import Device as NbDevice
    from dcim.models import Interface as NbInterface
    from dcim.filtersets import DeviceFilterSet
    from ipam.models import IPAddress as NbIPAddress
except ImportError:
    NbDevice = None
    NbInterface = None
    DeviceFilterSet = None
    NbIPAddress = None

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


def _enrich_sightings(results, backends, vm_config):
    """Bring search sightings to parity with MAC History: interface link +
    port_description (free — same device+port lookup as the URL), wired vs
    wireless classification (wireless_ports(), one bulk call per distinct
    device, no per-port fan-out), and a real "active now" (oper=up, from
    VictoriaMetrics) — unlike Netdisco's own "active" flag on a sighting,
    which just means "not archived/superseded", not "recently confirmed";
    since search() already filters to non-archived sightings by default,
    that flag is trivially "yes" for every row and tells the user nothing.
    """
    if not NbDevice:
        return
    sightings = [s for r in results or [] for s in (r.sightings or [])]
    ips = {s["switch"] for s in sightings if s.get("switch")}
    if not ips:
        return apply_ap_radio_links(sightings)
    q = reduce(operator.or_, (Q(primary_ip4__address__net_host=ip) for ip in ips))
    device_map = {
        str(d.primary_ip4.address.ip): d
        for d in NbDevice.objects.filter(q).select_related("primary_ip4")
    }
    if not device_map:
        return apply_ap_radio_links(sightings)

    iface_map = {}
    if NbInterface:
        for iface in NbInterface.objects.filter(device__in=device_map.values()).only(
            "device_id", "name", "description"
        ):
            iface_map[(iface.device_id, iface.name)] = (iface.get_absolute_url(), iface.description)

    wireless_by_ip = {}
    if backends:
        with ThreadPoolExecutor() as executor:
            futures = {executor.submit(backends[0].wireless_ports, ip): ip for ip in device_map}
            for future in as_completed(futures):
                wireless_by_ip[futures[future]] = future.result()

    for s in sightings:
        d = device_map.get(s.get("switch"))
        if not d:
            continue
        s["nb_device_url"] = d.get_absolute_url()
        s["device_id"] = d.pk
        port = s.get("port")
        if port:
            url, description = iface_map.get((d.pk, port), (None, None))
            s["nb_interface_url"] = url
            s["port_description"] = description or None
            ports = wireless_by_ip.get(s["switch"])
            if ports is not None:
                s["is_wireless"] = port in ports
    apply_ap_radio_links(sightings)



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
        return apply_ap_radio_links(rows)
    q = reduce(operator.or_, (Q(primary_ip4__address__net_host=ip) for ip in ips))
    device_map = {
        str(d.primary_ip4.address.ip): d
        for d in NbDevice.objects.filter(q).select_related("primary_ip4")
    }
    iface_map = {}
    if NbInterface and device_map:
        # description comes along for free. A WLC's radio "ports" have no
        # interface here any more -- apply_ap_radio_links resolves those to the AP.
        qs = NbInterface.objects.filter(device__in=device_map.values())
        for iface in qs.only("device_id", "name", "description"):
            iface_map[(iface.device_id, iface.name)] = (iface.get_absolute_url(), iface.description)
    for r in rows:
        d = device_map.get(r.get("device_ip"))
        if d:
            r["nb_device_url"] = d.get_absolute_url()
            r["area"] = d.cf.get("service_group")
            r["device_name"] = r.get("device_name") or d.name
            r["device_id"] = d.pk
            if r.get("port"):
                url, description = iface_map.get((d.pk, r["port"]), (None, None))
                r["nb_interface_url"] = url
                r["port_description"] = description or None
    apply_ap_radio_links(rows)


def _apply_wireless(rows, backends):
    """Classify each row wired vs wireless via device_port_wireless (one
    Netdisco call per distinct device — no per-port fan-out): a node's port
    is wireless iff it's in that device's radio-port set (WLC nodes report
    port as "<AP-MAC>.<radio-index>", so membership is a stable, direct
    match, not a guess).

    Leaves is_wireless unset (rendered as "—") only when the Netdisco call
    itself failed — a device confirmed to have zero radio ports (an
    ordinary switch) gets is_wireless=False for every row, not "—", since
    there's no ambiguity: nothing on it can be wireless.
    """
    if not backends:
        return
    device_ips = {r["device_ip"] for r in rows if r.get("device_ip") and r.get("port")}
    if not device_ips:
        return
    wireless_by_device = {}
    with ThreadPoolExecutor() as executor:
        futures = {executor.submit(backends[0].wireless_ports, ip): ip for ip in device_ips}
        for future in as_completed(futures):
            ip = futures[future]
            wireless_by_device[ip] = future.result()
    for r in rows:
        ports = wireless_by_device.get(r.get("device_ip"))
        if ports is not None:
            r["is_wireless"] = r.get("port") in ports


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
            wildcard = "*" in query or "%" in query
            date_from = form.cleaned_data.get("date_from")
            date_to = form.cleaned_data.get("date_to")
            # Searches without an explicit date range stay active-only — a
            # wildcard combined with a full archived scan is expensive on
            # Netdisco's side. An explicitly picked range is honored.
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
                        executor.submit(b.search, query, False, archived, since, until): i
                        for i, b in enumerate(backends)
                    }
                    for future in as_completed(futures):
                        results[futures[future]] = future.result()

                _enrich_results(results)
                _enrich_sightings(results, backends, config.get("victoria_metrics", {}))
                for r in results:
                    if r:
                        apply_transit_ports(r.sightings or [])
                context["results"] = results
                context["query"] = query
                context["query_note"] = form.cleaned_data.get("q_note")
                context["wildcard_active_only"] = wildcard and not archived
                context["archived"] = archived
                # The port timeline spans the picked range, not first sighting -> now.
                context["range_from"] = since
                context["range_to"] = until
                context["history_from"] = (date.today() - timedelta(days=30)).isoformat()
                context["history_to"] = date.today().isoformat()

        if htmx_partial(request):
            return render(request, "netbox_lens/search_results.html", context)

        return render(request, "netbox_lens/search.html", context)


class LensSearchHistoryView(PermissionRequiredMixin, View):
    """htmx partial behind "Load history": the archived port sightings of the
    MACs a search found, replacing that backend's sightings block."""
    permission_required = "netbox_lens.use_lens"

    def get(self, request):
        config = settings.PLUGINS_CONFIG.get("netbox_lens", {})
        backend = next((b for b in get_backends(config) if b.name == request.GET.get("backend")), None)
        macs = [m for m in request.GET.getlist("mac") if m]
        if not backend or not macs:
            return HttpResponse("")
        sightings, error = backend.sighting_history(macs)
        result = SearchResult(backend=backend.name, label=backend.label, icon=backend.icon,
                              sightings=sightings, history_macs=macs)
        _enrich_sightings([result], [backend], config.get("victoria_metrics", {}))
        apply_transit_ports(result.sightings or [])
        return render(request, "netbox_lens/search_sightings.html", {
            "backend": result, "history": True, "history_error": error,
        })


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
            kwargs["auth_profile"] = device.cf.get("snmp_auth_profile") or None
            kwargs["snmp_timeout"] = device.cf.get("snmp_polling_timeout") or None

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


def _ip_holders(entries):
    """For refresh-ip errors where the IP is already assigned elsewhere:
    {ip: {ip_url, device, device_url, interface, interface_url}} from NetBox,
    so the page can say "already on <device> <interface>" with links instead
    of discobox's free-text reason."""
    ips = {e["new"] for e in entries if e.get("action") == "error" and e.get("new")}
    if not NbIPAddress or not ips:
        return {}
    q = reduce(operator.or_, (Q(address__net_host=ip) for ip in ips))
    holders = {}
    for obj in NbIPAddress.objects.filter(q).exclude(assigned_object_id=None):
        owner = obj.assigned_object
        parent = getattr(owner, "device", None) or getattr(owner, "virtual_machine", None)
        holders[str(obj.address.ip)] = {
            "ip_url": obj.get_absolute_url(),
            # discobox can move it only off a device interface, not a VM's
            "movable": isinstance(owner, NbInterface) if NbInterface else False,
            "device": getattr(parent, "name", None),
            "device_url": parent.get_absolute_url() if parent else None,
            "interface": getattr(owner, "name", None),
            "interface_url": owner.get_absolute_url() if owner else None,
        }
    return holders


def _with_holders(entries):
    holders = _ip_holders(entries)
    return [dict(e, holder=holders.get(e.get("new"))) if e.get("action") == "error" else e for e in entries]


def _refresh_ip_excluded_roles():
    return list(settings.PLUGINS_CONFIG.get("netbox_lens", {}).get("refresh_ip_exclude_roles") or ())


# Virtual chassis members other than the master: the chassis' IP sits on the
# master, and a member's name resolving to it would offer to move it away.
VC_NON_MASTER = Q(virtual_chassis__isnull=False) & ~Q(virtual_chassis__master=F("pk"))


def _is_vc_non_master(device):
    vc = device.virtual_chassis
    return vc is not None and vc.master_id != device.pk


class LensRefreshIpView(PermissionRequiredMixin, View):
    """Preview (dry run) and apply of discobox's /refresh-ip for one device:
    sets primary_ip4 from DNS (else Netdisco), for devices that lack one."""
    permission_required = "netbox_lens.trigger_lens"

    def post(self, request, pk):
        device = get_object_or_404(NbDevice.objects.select_related("role", "virtual_chassis"), pk=pk)
        dry_run = request.POST.get("dry_run", "true") != "false"
        move = request.POST.get("move") == "true"
        refusal = None
        if device.role and device.role.slug in _refresh_ip_excluded_roles():
            refusal = f"Refresh IP is disabled for role {device.role.slug}."
        elif _is_vc_non_master(device):
            refusal = "This device is a virtual chassis member; the chassis IP belongs on its master."
        if refusal:
            return render(request, "netbox_lens/refresh_ip_modal.html", {
                "device": device, "dry_run": dry_run, "error": refusal,
            })
        config = settings.PLUGINS_CONFIG.get("netbox_lens", {}).get("discobox", {})
        ok, data, error = refresh_ip(config, device.pk, dry_run=dry_run, move=move)
        entry = next(iter((data or {}).get("devices") or []), None)
        if ok and not entry:
            ok, error = False, "Discobox returned no result for this device."
        elif ok and entry.get("action") == "error":
            entry = _with_holders([entry])[0]
            if not entry.get("holder"):
                ok, error = False, entry.get("error") or "Discobox could not refresh the IP."
        return render(request, "netbox_lens/refresh_ip_modal.html", {
            "device": device, "dry_run": dry_run, "error": None if ok else error, "entry": entry,
        })


# Bulk Refresh IPs: discobox answers synchronously (a DNS lookup per device),
# so cap how many devices one preview may cover.
REFRESH_IPS_MAX = 500
# Device list parameters that aren't filters.
_NON_FILTER_PARAMS = {"page", "per_page", "sort", "ordering", "export", "return_url", "tab", "embedded"}


def _refresh_ips_filter(request):
    """The device filter from the query string, as NetBox's device list sends it."""
    params = request.GET.copy()
    for key in list(params):
        if key in _NON_FILTER_PARAMS or not params.getlist(key) or not any(params.getlist(key)):
            del params[key]
    return params


# Netdisco Sync preset: devices whose last sync is older than this.
SYNC_STALE_DAYS = 30


def _existing_roles(slugs):
    """The slugs that exist as device roles here — a filter on a missing role
    slug fails as a whole, so presets only use the ones that exist."""
    from dcim.models import DeviceRole
    found = set(DeviceRole.objects.filter(slug__in=slugs).values_list("slug", flat=True))
    return [s for s in slugs if s in found]


def _bulk_presets(tab, current_qs):
    """Preset filters for a bulk tab as [{label, qs, active}]. Built per
    request, so a relative date ("older than 30 days") is always from today."""
    def qs(*pairs):
        q = QueryDict(mutable=True)
        for k, v in pairs:
            q.appendlist(k, v)
        return q.urlencode()

    presets = []
    if tab == "refresh_ips":
        base = (("has_primary_ip", "false"), ("status", "active"))
        presets.append((_("All without IP"), qs(*base)))
        infra = [r for r in settings.PLUGINS_CONFIG.get("netbox_lens", {}).get("device_panel_roles") or []
                 if r not in _refresh_ip_excluded_roles()]
        from dcim.models import DeviceRole
        names = dict(DeviceRole.objects.filter(slug__in=infra).values_list("slug", "name"))
        for role in infra:
            if role in names:
                presets.append((names[role], qs(*base, ("role", role))))
    else:
        base = (("has_primary_ip", "true"), ("status", "active"))
        skip = tuple(("role__n", r) for r in _existing_roles(_refresh_ip_excluded_roles()))
        stale = (timezone.localdate() - timedelta(days=SYNC_STALE_DAYS)).isoformat()
        presets += [
            (_("Switches"), qs(*base, ("role", "switch"))) if _existing_roles(["switch"]) else None,
            (_("Not synced for %(days)d days") % {"days": SYNC_STALE_DAYS},
             # __lt alone also matches devices never synced: those have their own preset
             qs(*base, *skip, ("cf_netdisco_last_update__empty", "false"), ("cf_netdisco_last_update__lt", stale))),
            (_("Never synced"), qs(*base, *skip, ("cf_netdisco_last_update__empty", "true"))),
        ]
    return [{"label": label, "qs": q, "active": q == current_qs} for label, q in filter(None, presets)]


def _bulk_devices(request, params):
    """(queryset, error) for a NetBox device filter, limited to what the user may view."""
    fs = DeviceFilterSet(params, queryset=NbDevice.objects.restrict(request.user, "view"))
    # a filterset ignores unknown keys, which would widen the filter to the whole fleet
    unknown = sorted(k for k in params if k not in fs.filters)
    if unknown:
        return None, f"Unknown device filter: {', '.join(unknown)}"
    if not fs.is_valid():
        return None, "; ".join(f"{k}: {', '.join(v)}" for k, v in fs.errors.items())
    return fs.qs, None


class LensRefreshIpsView(PermissionRequiredMixin, View):
    """Bulk Refresh IPs: preview discobox /refresh-ip for every device matching
    a NetBox device filter, then apply it to the devices ticked. LENS resolves
    the filter itself and sends device ids, so preview and apply cover exactly
    the same devices."""
    permission_required = "netbox_lens.trigger_lens"

    def get(self, request):
        params = _refresh_ips_filter(request)
        if not params:
            # Default: active switches without primary IPv4. One role only: a
            # role slug missing on this NetBox fails the whole filter.
            params = QueryDict(mutable=True)
            params["has_primary_ip"] = "false"
            params["status"] = "active"
            params["role"] = "switch"
            return redirect(f"{request.path}?{params.urlencode()}")
        context = {"filter_params": params, "filter_qs": params.urlencode(), "dry_run": True, "bulk_tab": "refresh_ips",
                   "presets": _bulk_presets("refresh_ips", params.urlencode())}
        devices, error = _bulk_devices(request, params)
        if error:
            context["error"] = error
            return render(request, "netbox_lens/refresh_ips.html", context)
        excluded = _refresh_ip_excluded_roles()
        qs = devices.exclude(role__slug__in=excluded)
        context["excluded_count"] = devices.filter(role__slug__in=excluded).count()
        context["vc_member_count"] = qs.filter(VC_NON_MASTER).count()
        qs = qs.exclude(VC_NON_MASTER)
        context["excluded_roles"] = excluded
        ids = list(qs.order_by("name").values_list("pk", flat=True)[:REFRESH_IPS_MAX + 1])
        context["matched"] = len(ids)
        if len(ids) > REFRESH_IPS_MAX:
            context["too_many"] = REFRESH_IPS_MAX
        elif ids:
            context.update(self._run(request, ids, dry_run=True))
        return render(request, "netbox_lens/refresh_ips.html", context)

    def post(self, request):
        filter_qs = request.POST.get("filter_qs", "")
        ids = [int(i) for i in request.POST.getlist("device") if i.isdigit()]
        # only devices this user may see, and never more than a preview could cover
        ids = list(NbDevice.objects.restrict(request.user, "view").filter(pk__in=ids[:REFRESH_IPS_MAX])
                   .exclude(role__slug__in=_refresh_ip_excluded_roles()).exclude(VC_NON_MASTER)
                   .values_list("pk", flat=True))
        context = {"filter_params": QueryDict(filter_qs), "filter_qs": filter_qs, "dry_run": False,
                   "matched": len(ids), "bulk_tab": "refresh_ips",
                   "presets": _bulk_presets("refresh_ips", filter_qs)}
        if not ids:
            context["error"] = "No devices selected."
        else:
            context.update(self._run(request, ids, dry_run=False))
        return render(request, "netbox_lens/refresh_ips.html", context)

    def _run(self, request, ids, dry_run):
        config = settings.PLUGINS_CONFIG.get("netbox_lens", {}).get("discobox", {})
        ok, data, error = refresh_ip(config, ids, dry_run=dry_run)
        if not ok:
            return {"error": error}
        urls = {d.pk: d.get_absolute_url() for d in NbDevice.objects.filter(pk__in=ids).only("pk")}
        entries = _with_holders([dict(e, url=urls.get(e.get("id"))) for e in (data or {}).get("devices") or []])
        # what changes first, errors next, unchanged last
        order = {"set": 0, "replace": 1, "error": 2, "unchanged": 3}
        entries.sort(key=lambda e: (order.get(e.get("action"), 9), e.get("device") or ""))
        return {"entries": entries, "summary": (data or {}).get("summary") or {}}


class LensBulkSyncView(PermissionRequiredMixin, View):
    """Bulk Sync: queue a discobox sync (Netdisco -> NetBox) for every device
    matching a NetBox device filter. discobox's /sync/all queues and returns
    at once; the devices are sent as id filters, so exactly those shown."""
    permission_required = "netbox_lens.trigger_lens"

    def get(self, request):
        params = _refresh_ips_filter(request)
        if not params:
            params = QueryDict(mutable=True)
            params["has_primary_ip"] = "true"
            params["status"] = "active"
            params["role"] = "switch"
            return redirect(f"{request.path}?{params.urlencode()}")
        return render(request, "netbox_lens/bulk_sync.html", self._context(request, params))

    def _context(self, request, params):
        context = {"filter_params": params, "filter_qs": params.urlencode(), "bulk_tab": "sync",
                   "can_force": request.user.is_superuser, "presets": _bulk_presets("sync", params.urlencode())}
        devices, error = _bulk_devices(request, params)
        if error:
            context["error"] = error
            return context
        context["no_ip_count"] = devices.filter(primary_ip4__isnull=True).count()
        rows = list(devices.filter(primary_ip4__isnull=False).select_related("primary_ip4", "role", "site")
                    .order_by("name")[:REFRESH_IPS_MAX + 1])
        context["matched"] = len(rows)
        if len(rows) > REFRESH_IPS_MAX:
            context["too_many"] = REFRESH_IPS_MAX
        else:
            context["rows"] = [{
                "id": d.pk, "name": d.name, "url": d.get_absolute_url(), "ip": _device_ip(d),
                "role": d.role.name if d.role else None, "site": d.site.name if d.site else None,
                "last_sync": d.cf.get("netdisco_last_update"),
            } for d in rows]
        return context

    def post(self, request):
        params = QueryDict(request.POST.get("filter_qs", ""))
        context = self._context(request, params)
        ids = [r["id"] for r in context.get("rows") or []]
        if context.get("error") or not ids:
            context.setdefault("error", "No devices to sync.")
            return render(request, "netbox_lens/bulk_sync.html", context)
        force = request.POST.get("force") == "true" and request.user.is_superuser
        config = settings.PLUGINS_CONFIG.get("netbox_lens", {}).get("discobox", {})
        ok, data, error = sync_all(config, ids, force=force)
        if ok and data and not force:
            # discobox drops recently synced devices before counting (unless forced)
            data["recent"] = max(0, len(ids) - (data.get("selected") or 0) - (data.get("not_in_netdisco") or 0))
        context.update({"result": data if ok else None, "result_error": error, "forced": force})
        return render(request, "netbox_lens/bulk_sync.html", context)


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
                _apply_wireless(rows, backends)
                apply_transit_ports(rows)
                if request.GET.get("export") == "csv":
                    return _csv_response(
                        "mac_history.csv",
                        [
                            ("Device", lambda r: r.get("device_name") or r.get("device_ip")),
                            ("Port", lambda r: r.get("port")),
                            ("Port Description", lambda r: r.get("port_description")),
                            ("Transit Via", lambda r: r.get("transit_via")),
                            ("MAC", lambda r: r.get("mac")),
                            ("VLAN", lambda r: r.get("vlan")),
                            ("Client IP", lambda r: r.get("client_ip")),
                            ("Client Name", lambda r: r.get("client_name")),
                            ("Active", lambda r: r.get("active")),
                            ("Connection", lambda r: "AP" if r.get("node_ap_url") else {True: "WLAN", False: "Wired"}.get(r.get("is_wireless"))),
                            ("SSID", lambda r: r.get("ssid")),
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
                            ("Active", lambda r: r.get("active")),
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


class LensDownPortsView(PermissionRequiredMixin, View):
    """Access ports free for reuse: no MAC seen for N days, from Netdisco's
    portfree report (one call for the whole fleet)."""
    permission_required = "netbox_lens.use_lens"

    def get(self, request):
        data = request.GET.copy()
        data.setdefault("min_days", "30")
        data.setdefault("admin", "any")
        data.setdefault("oper", "down")
        # all bases by default; none ticked means no filter, as in the report
        if not data.getlist("basis"):
            data.setlist("basis", [value for value, _ in BASIS_CHOICES])
        form = DownPortsForm(data)
        context = {"form": form}

        if form.is_valid():
            config = settings.PLUGINS_CONFIG.get("netbox_lens", {})
            backend = next((b for b in get_backends(config) if hasattr(b, "free_ports")), None)
            cd = form.cleaned_data
            if backend is None:
                rows, total, truncated, limited, error = [], 0, False, False, "Netdisco backend not configured"
            else:
                rows, total, truncated, limited, error = build_down_ports(
                    backend,
                    min_days=cd["min_days"],
                    oper=cd["oper"],
                    basis=cd.get("basis") or None,
                    admin=cd["admin"],
                    device_query=cd.get("device") or None,
                    interface_query=cd.get("interface") or None,
                    description_query=cd.get("description") or None,
                    vlan_query=cd.get("vlan") or None,
                    grafana_template=config.get("grafana_interface_url"),
                )

            if request.GET.get("export") == "csv" and not error:
                return _csv_response(
                    "down-ports.csv",
                    [
                        ("Element name", lambda r: r.get("device_name")),
                        ("Interface (ifDescr)", lambda r: r.get("interface_name")),
                        ("Description (ifAlias)", lambda r: r.get("description")),
                        ("VLAN", lambda r: r.get("vlan")),
                        ("Speed", lambda r: r.get("speed")),
                        ("If admin.", lambda r: r.get("admin")),
                        ("If oper.", lambda r: r.get("oper")),
                        ("Days unused", lambda r: r.get("age_days")),
                        ("Unused since", lambda r: r.get("free_since")),
                        ("Days unused based on", lambda r: {"mac": "last MAC", "lastchange": "link change", "boot": "at least since boot"}.get(r.get("age_basis"), "unknown")),
                        ("Last MAC seen", lambda r: r.get("last_mac_seen")),
                        ("Last MAC", lambda r: r.get("last_mac")),
                        ("Last macsuck", lambda r: r.get("last_macsuck")),
                        ("Last discover", lambda r: r.get("last_discover")),
                        ("Link last change", lambda r: r.get("lastchange_stamp")),
                        ("Device boot", lambda r: r.get("device_boot")),
                    ],
                    rows,
                )

            context.update({
                "rows": rows,
                "total": total,
                "truncated": truncated,
                "limited": limited,
                "error": error,
                "searched": True,
                "min_days": cd["min_days"],
            })

        return render(request, "netbox_lens/down_ports.html", context)


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
            label="MAC Table",
            visible=_is_switch_or_router,
            permission="netbox_lens.use_lens",
        )


MAX_JOB_POLLS = 60  # at 5s each: 5 minutes


if NbDevice:
    @register_model_view(NbDevice, name="lens_jobs", path="lens-jobs")
    class DeviceNetdiscoJobsView(ObjectView):
        """Tab shell only, same as DeviceMacArpView: the Netdisco call happens
        in LensDeviceJobsDataView via htmx after the page has rendered."""
        queryset = NbDevice.objects.all()
        additional_permissions = ["netbox_lens.jobs_lens"]
        template_name = "netbox_lens/device_jobs.html"
        tab = ViewTab(
            label="Netdisco Jobs",
            visible=lambda device: device.primary_ip4_id is not None and device_panel_enabled(
                device, settings.PLUGINS_CONFIG.get("netbox_lens", {})
            ),
            permission="netbox_lens.jobs_lens",
            weight=2100,
        )


if NbDevice:
    from dcim.filtersets import DeviceFilterSet
    from dcim.forms import DeviceFilterForm
    from dcim.tables import DeviceTable
    from netbox.views.generic import ObjectChildrenView

    def _wlc_controller_filter(device):
        cf_name = settings.PLUGINS_CONFIG.get("netbox_lens", {}).get("wlc_controller_cf") or "controller"
        return {f"custom_field_data__{cf_name}": device.pk}

    @register_model_view(NbDevice, name="lens_aps", path="lens-aps")
    class DeviceControllerAPsView(ObjectChildrenView):
        """The APs discobox's WLC sync assigned to this controller (object CF
        controller -> this device), as a regular NetBox device table.
        No badge: counting would run on every WLC page load."""
        queryset = NbDevice.objects.all()
        child_model = NbDevice
        table = DeviceTable
        filterset = DeviceFilterSet
        filterset_form = DeviceFilterForm
        tab = ViewTab(
            label="APs",
            visible=lambda device: bool(device.role) and device.role.slug == "lwapp-ctr",
            permission="dcim.view_device",
            weight=2050,
        )

        def get_children(self, request, parent):
            return NbDevice.objects.restrict(request.user, "view").filter(**_wlc_controller_filter(parent))


class LensDeviceJobsDataView(PermissionRequiredMixin, View):
    """htmx partial backing DeviceNetdiscoJobsView. Re-polls itself while any
    job is still queued/running, and stops once they've all finished — or
    after MAX_JOB_POLLS rounds, since a job stuck in the queue (no backend
    picking it up) would otherwise keep an open tab polling forever."""
    permission_required = "netbox_lens.jobs_lens"

    def get(self, request, pk):
        try:
            polls = int(request.GET.get("polls", 0))
        except ValueError:
            polls = 0
        device = get_object_or_404(NbDevice, pk=pk)
        if not device_panel_enabled(device, settings.PLUGINS_CONFIG.get("netbox_lens", {})):
            return HttpResponse("")
        ip = _device_ip(device)
        jobs = None
        if ip:
            backends = get_backends(settings.PLUGINS_CONFIG.get("netbox_lens", {}))
            if backends:
                jobs = backends[0].device_jobs(ip)
        return render(request, "netbox_lens/device_jobs_data.html", {
            "jobs": jobs,
            "lens_device_ip": ip,
            "lens_device_pk": device.pk,
            "pending": any(j["status"] in ("queued", "running") for j in jobs or []),
            "next_poll": polls + 1 if polls < MAX_JOB_POLLS else None,
        })


class LensDevicePanelDataView(PermissionRequiredMixin, View):
    """htmx partial for the device page's LENS panel: stats, LENS Detail and
    neighbors — the Netdisco-backed part, loaded after the page itself."""
    permission_required = "netbox_lens.use_lens"

    def get(self, request, pk):
        device = get_object_or_404(NbDevice.objects.select_related("role", "primary_ip4"), pk=pk)
        if not device_panel_enabled(device, settings.PLUGINS_CONFIG.get("netbox_lens", {})):
            return HttpResponse("")
        return render(request, "netbox_lens/device_panel_data.html", device_panel_context(device, request.user))


class LensInterfaceNodesDataView(PermissionRequiredMixin, View):
    """htmx partial for the interface page's LENS panel (active MACs on the port)."""
    permission_required = "netbox_lens.use_lens"

    def get(self, request, pk):
        iface = get_object_or_404(NbInterface.objects.select_related("device__primary_ip4"), pk=pk)
        return render(request, "netbox_lens/interface_nodes_data.html", interface_nodes_context(iface))


class LensInterfacePortDataView(PermissionRequiredMixin, View):
    """htmx partial for the interface page's LENS panel (Netdisco port info)."""
    permission_required = "netbox_lens.use_lens"

    def get(self, request, pk):
        iface = get_object_or_404(NbInterface.objects.select_related("device__primary_ip4"), pk=pk)
        return render(request, "netbox_lens/interface_port_data.html", interface_port_context(iface))


def _port_sort_key(port):
    """Natural order: Gi1/0/2 before Gi1/0/10."""
    return [(0, int(p)) if p.isdigit() else (1, p.lower()) for p in re.split(r"(\d+)", port or "")]


def _group_by_port(rows):
    """Sort rows by port (newest first within a port) and set port_rowspan on
    each group's first row (0 on the rest), so the table shows a port once.
    Transit rows of a port hide together, so the rowspan stays consistent."""
    rows.sort(key=lambda r: r.get("time_last") or "", reverse=True)
    rows.sort(key=lambda r: _port_sort_key(r.get("port")))
    for _, group in groupby(rows, key=lambda r: r.get("port")):
        group = list(group)
        for r in group:
            r["port_rowspan"] = 0
        group[0]["port_rowspan"] = len(group)


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
            apply_transit_ports(rows)
            iface_map = {}
            if NbInterface:
                iface_map = {
                    iface.name: (iface.get_absolute_url(), iface.description)
                    for iface in NbInterface.objects.filter(device=device).only("device_id", "name", "description")
                }
            for r in rows:
                r["device_name"] = device.name
                r["area"] = device.cf.get("service_group")
                if r.get("port"):
                    url, description = iface_map.get(r["port"], (None, None))
                    r["nb_interface_url"] = url
                    r["port_description"] = description or None
            _group_by_port(rows)
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
