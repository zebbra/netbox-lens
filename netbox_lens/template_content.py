import operator
from concurrent.futures import ThreadPoolExecutor, as_completed
from functools import reduce

from django.conf import settings
from django.db.models import Q
from netbox.plugins import PluginTemplateExtension

from .backends import get_backends

try:
    from dcim.models import Device as NbDevice
except ImportError:
    NbDevice = None


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


def _device_neighbors(backends, device_ip):
    neighbors = []
    with ThreadPoolExecutor() as executor:
        futures = [executor.submit(b.device_neighbors, device_ip) for b in backends]
        for future in as_completed(futures):
            neighbors.extend(future.result())

    if NbDevice:
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

    return neighbors


def _device_web_links(backends, device_ip):
    links = []
    for b in backends:
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
        nodes = [n for n in _device_nodes(backends, ip) if n.get("active")]
        neighbors = _device_neighbors(backends, ip)
        stats = {
            "macs": len(nodes),
            "ports": len({n["port"] for n in nodes if n.get("port")}),
            "vlans": len({n["vlan"] for n in nodes if n.get("vlan") and n["vlan"] != "0"}),
            "neighbors": len(neighbors),
        }
        summaries = _device_summaries(backends, ip)
        return self.render("netbox_lens/device_nodes_panel.html", extra_context={
            "lens_stats": stats,
            "lens_device_ip": ip,
            "lens_summaries": summaries,
            "lens_backends": [b.label for b in backends],
            "lens_web_links": _device_web_links(backends, ip),
            "lens_neighbors": neighbors,
            "lens_can_trigger": self.context["request"].user.has_perm("netbox_lens.trigger_lens"),
            "lens_device_pk": self.context["object"].pk,
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
