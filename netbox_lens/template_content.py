import operator
from concurrent.futures import ThreadPoolExecutor, as_completed
from functools import reduce

from django.conf import settings
from django.db.models import Q
from netbox.plugins import PluginTemplateExtension

from .backends import get_backends

try:
    from dcim.models import Device as NbDevice
    from dcim.models import Interface as NbInterface
except ImportError:
    NbDevice = None
    NbInterface = None


def _device_nodes(backends, device_ip, port=None, since=None, until=None):
    nodes = []
    with ThreadPoolExecutor() as executor:
        futures = [executor.submit(b.device_nodes, device_ip, since, until) for b in backends]
        for future in as_completed(futures):
            result = future.result()
            if port:
                result = [n for n in result if n.get("port") == port]
            nodes.extend(result)
    return nodes


def _device_summaries(backends, device_ip):
    summaries = []
    with ThreadPoolExecutor() as executor:
        futures = {executor.submit(b.device_summary, device_ip): b for b in backends}
        for future in as_completed(futures):
            summary = future.result()
            if summary:
                summaries.append((futures[future].label, summary))
    return summaries


def _device_neighbors(backends, device_ip, device=None):
    neighbors = []
    with ThreadPoolExecutor() as executor:
        futures = [executor.submit(b.device_neighbors, device_ip) for b in backends]
        for future in as_completed(futures):
            neighbors.extend(future.result())

    if NbInterface and device and neighbors:
        local_ports = {n["port"] for n in neighbors if n.get("port")}
        if local_ports:
            iface_map = {
                iface.name: iface.get_absolute_url()
                for iface in NbInterface.objects.filter(device=device, name__in=local_ports).only("name")
            }
            for n in neighbors:
                if n.get("port") in iface_map:
                    n["nb_local_port_url"] = iface_map[n["port"]]

    if NbDevice and neighbors:
        ips = {n["remote_ip"] for n in neighbors if n.get("remote_ip")}
        if ips:
            q = reduce(operator.or_, (Q(primary_ip4__address__net_host=ip) for ip in ips))
            url_map = {
                str(d.primary_ip4.address.ip): d.get_absolute_url()
                for d in NbDevice.objects.filter(q).select_related("primary_ip4")
            }
            for n in neighbors:
                if n.get("remote_ip") in url_map:
                    n["nb_device_url"] = url_map[n["remote_ip"]]

        # Fallback for neighbors that didn't resolve by IP: Netdisco's CDP/LLDP
        # remote_id is often the bare hostname while NetBox device names are
        # FQDNs, so match on a name prefix instead of an exact name.
        missing = [n for n in neighbors if not n.get("nb_device_url") and n.get("remote_id")]
        rids = {n["remote_id"].strip().rstrip(".") for n in missing if n["remote_id"].strip()}
        if rids:
            q = reduce(operator.or_, (Q(name__istartswith=rid) for rid in rids))
            candidates = list(NbDevice.objects.filter(q).order_by("name"))
            name_url_map = {}
            for rid in rids:
                rid_lower = rid.lower()
                match = next((d for d in candidates if d.name.lower().startswith(rid_lower)), None)
                if match:
                    name_url_map[rid] = match.get_absolute_url()
            for n in missing:
                rid = n["remote_id"].strip().rstrip(".")
                if rid in name_url_map:
                    n["nb_device_url"] = name_url_map[rid]

    return neighbors


def _device_web_links(backends, device_ip, found_labels):
    """Only link out to backends that actually have this device — device_web_url()
    just formats a URL from the IP, so linking unconditionally would offer a
    dead link for devices a backend has never discovered."""
    links = []
    for b in backends:
        if b.label not in found_labels:
            continue
        url = b.device_web_url(device_ip)
        if url:
            links.append((b.label, url))
    return links


def _device_ip(device):
    if not device.primary_ip4:
        return None
    return str(device.primary_ip4.address.ip)


class DeviceLensPanel(PluginTemplateExtension):
    models = ["dcim.device"]

    def right_page(self):
        if not self.context["request"].user.has_perm("netbox_lens.use_lens"):
            return ""
        ip = _device_ip(self.context["object"])
        if not ip:
            return ""
        config = settings.PLUGINS_CONFIG.get("netbox_lens", {})
        backends = get_backends(config)
        if not backends:
            return ""
        device = self.context["object"]
        nodes = [n for n in _device_nodes(backends, ip) if n.get("active")]
        neighbors = _device_neighbors(backends, ip, device=device)
        stats = {
            "macs": len(nodes),
            "ports": len({n["port"] for n in nodes if n.get("port")}),
            "vlans": len({n["vlan"] for n in nodes if n.get("vlan") and n["vlan"] != "0"}),
            "neighbors": len(neighbors),
        }
        summaries = _device_summaries(backends, ip)
        found_labels = {label for label, _ in summaries}
        return self.render("netbox_lens/device_nodes_panel.html", extra_context={
            "lens_stats": stats,
            "lens_found": bool(summaries),
            "lens_device_ip": ip,
            "lens_summaries": summaries,
            "lens_backends": [b.label for b in backends],
            "lens_web_links": _device_web_links(backends, ip, found_labels),
            "lens_neighbors": neighbors,
            "lens_can_trigger": self.context["request"].user.has_perm("netbox_lens.trigger_lens"),
            "lens_is_superuser": self.context["request"].user.is_superuser,
            "lens_device_pk": device.pk,
            "lens_bossy_last_updated": device.cf.get("bossy_last_updated"),
            "lens_netdisco_last_update": device.cf.get("netdisco_last_update"),
            "lens_snmp_modulator_last_updated": device.cf.get("snmp_modulator_last_updated"),
        })


class InterfaceLensPanel(PluginTemplateExtension):
    models = ["dcim.interface"]

    def full_width_page(self):
        if not self.context["request"].user.has_perm("netbox_lens.use_lens"):
            return ""
        iface = self.context["object"]
        ip = _device_ip(iface.device)
        if not ip:
            return ""
        config = settings.PLUGINS_CONFIG.get("netbox_lens", {})
        backends = get_backends(config)
        if not backends:
            return ""
        nodes = [n for n in _device_nodes(backends, ip, port=iface.name) if n.get("active")]
        return self.render("netbox_lens/device_nodes_panel.html", extra_context={
            "lens_nodes": nodes,
            "lens_device_ip": ip,
            "lens_port": iface.name,
        })


template_extensions = [DeviceLensPanel, InterfaceLensPanel]
