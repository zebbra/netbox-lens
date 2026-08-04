import operator
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, timedelta
from functools import reduce

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.mixins import PermissionRequiredMixin
from django.db.models import Q
from django.shortcuts import get_object_or_404, redirect, render
from django.views import View
from netbox.views.generic import ObjectView
from utilities.htmx import htmx_partial
from utilities.views import ViewTab, register_model_view

from .backends import get_backends
from .forms import NodeSearchForm
from .template_content import _device_nodes

try:
    from dcim.models import Device as NbDevice
except ImportError:
    NbDevice = None

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


class LensStatusView(PermissionRequiredMixin, View):
    permission_required = "netbox_lens.use_lens"

    def get(self, request):
        config = settings.PLUGINS_CONFIG.get("netbox_lens", {})
        backends = get_backends(config)
        statuses = []
        with ThreadPoolExecutor() as executor:
            futures = {executor.submit(b.status): b for b in backends}
            for future in as_completed(futures):
                statuses.append(future.result())
        return render(request, "netbox_lens/status.html", {
            "statuses": statuses,
            "config_error": None if backends else (
                "No backends configured. Add at least one backend to "
                "PLUGINS_CONFIG['netbox_lens']['backends']."
            ),
        })


class LensSearchView(PermissionRequiredMixin, View):
    permission_required = "netbox_lens.use_lens"

    def get(self, request):
        form = NodeSearchForm(request.GET or None)
        context = {"form": form}

        if form.is_valid():
            query = form.cleaned_data["q"]
            partial = form.cleaned_data.get("partial", False)
            since_choice = form.cleaned_data.get("since") or "week"
            _since_map = {
                "now":    (None, False),
                "week":   (date.today() - timedelta(days=7), True),
                "2weeks": (date.today() - timedelta(days=14), True),
                "month":  (date.today() - timedelta(days=30), True),
            }
            since_date, archived = _since_map.get(since_choice, _since_map["week"])
            since = since_date.isoformat() if since_date else None

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
                        executor.submit(b.search, query, partial, archived, since): i
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


class LensDiscoverView(PermissionRequiredMixin, View):
    permission_required = "netbox_lens.trigger_lens"

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

        for backend in backends:
            success, message = backend.trigger_discover(ip)
            if success:
                messages.success(request, message)
            else:
                messages.error(request, message)

        return redirect(device.get_absolute_url())


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
                return {"lens_nodes": [], "lens_device_ip": None, "lens_total_nodes": 0, "lens_truncated": False}

            config = settings.PLUGINS_CONFIG.get("netbox_lens", {})
            backends = get_backends(config)
            nodes = sorted(_device_nodes(backends, ip), key=lambda n: n.get("time_last") or "", reverse=True)
            return {
                "lens_nodes": nodes[:MAX_MACARP_ROWS],
                "lens_device_ip": ip,
                "lens_total_nodes": len(nodes),
                "lens_truncated": len(nodes) > MAX_MACARP_ROWS,
            }
