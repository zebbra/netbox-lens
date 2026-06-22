from concurrent.futures import ThreadPoolExecutor, as_completed

from django.conf import settings
from netbox.plugins import PluginTemplateExtension

from .backends import get_backends


def _device_nodes(backends, device_ip, port=None):
    nodes = []
    with ThreadPoolExecutor() as executor:
        futures = [executor.submit(b.device_nodes, device_ip) for b in backends]
        for future in as_completed(futures):
            result = future.result()
            if port:
                result = [n for n in result if n.get("port") == port]
            nodes.extend(result)
    return nodes


def _device_ip(device):
    if not device.primary_ip4:
        return None
    return str(device.primary_ip4.address.ip)


class DeviceLensPanel(PluginTemplateExtension):
    models = ["dcim.device"]

    def full_width_page(self):
        ip = _device_ip(self.context["object"])
        if not ip:
            return ""
        config = settings.PLUGINS_CONFIG.get("netbox_lens", {})
        backends = get_backends(config)
        if not backends:
            return ""
        nodes = [n for n in _device_nodes(backends, ip) if n.get("active")]
        stats = {
            "macs": len(nodes),
            "ports": len({n["port"] for n in nodes if n.get("port")}),
            "vlans": len({n["vlan"] for n in nodes if n.get("vlan") and n["vlan"] != "0"}),
        }
        return self.render("netbox_lens/device_nodes_panel.html", extra_context={
            "lens_stats": stats,
            "lens_device_ip": ip,
        })


class InterfaceLensPanel(PluginTemplateExtension):
    models = ["dcim.interface"]

    def full_width_page(self):
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
