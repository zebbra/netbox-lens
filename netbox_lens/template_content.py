import operator
import re
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

try:
    from dcim.models import MACAddress as NbMACAddress  # NetBox >= 4.2
except ImportError:
    NbMACAddress = None


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


def _fetch_neighbors(backends, device_ip):
    neighbors = []
    with ThreadPoolExecutor() as executor:
        futures = [executor.submit(b.device_neighbors, device_ip) for b in backends]
        for future in as_completed(futures):
            neighbors.extend(future.result())
    return neighbors


def _set_neighbor_device(neighbor, device):
    neighbor["nb_device_url"] = device.get_absolute_url()
    neighbor["nb_device_name"] = device.name


def _link_neighbors(neighbors, device=None):
    """Attach NetBox URLs for the local port and the remote device. ORM work,
    so it runs on the request thread, not in the fetch pool."""
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
            device_map = {
                str(d.primary_ip4.address.ip): d
                for d in NbDevice.objects.filter(q).select_related("primary_ip4")
            }
            for n in neighbors:
                if n.get("remote_ip") in device_map:
                    _set_neighbor_device(n, device_map[n["remote_ip"]])

        # remote_id is often the LLDP chassis MAC rather than a hostname: look
        # it up among NetBox's interface MACs, and link only if it's on
        # exactly one device (shared/virtual MACs stay unresolved).
        missing = [n for n in neighbors if not n.get("nb_device_url") and _normalize_mac(n.get("remote_id"))]
        if NbMACAddress and missing:
            macs = {_normalize_mac(n["remote_id"]) for n in missing}
            devices_by_mac = {}
            for mac, device_id in NbMACAddress.objects.filter(
                mac_address__in=macs, interface__isnull=False,
            ).values_list("mac_address", "interface__device_id"):
                devices_by_mac.setdefault(str(mac).lower(), set()).add(device_id)
            unique = {mac: ids.pop() for mac, ids in devices_by_mac.items() if len(ids) == 1}
            devices = NbDevice.objects.in_bulk(set(unique.values()))
            for n in missing:
                device_id = unique.get(_normalize_mac(n["remote_id"]))
                if device_id in devices:
                    _set_neighbor_device(n, devices[device_id])

        # Fallback for neighbors that didn't resolve by IP: Netdisco's CDP/LLDP
        # remote_id is often the bare hostname while NetBox device names are
        # FQDNs, so match on a name prefix instead of an exact name.
        missing = [n for n in neighbors if not n.get("nb_device_url") and n.get("remote_id")]
        rids = {n["remote_id"].strip().rstrip(".") for n in missing if n["remote_id"].strip()}
        if rids:
            q = reduce(operator.or_, (Q(name__istartswith=rid) for rid in rids))
            candidates = list(NbDevice.objects.filter(q).order_by("name"))
            name_map = {}
            for rid in rids:
                rid_lower = rid.lower()
                match = next((d for d in candidates if d.name.lower().startswith(rid_lower)), None)
                if match:
                    name_map[rid] = match
            for n in missing:
                rid = n["remote_id"].strip().rstrip(".")
                if rid in name_map:
                    _set_neighbor_device(n, name_map[rid])

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


# LLDP chassis IDs sent as a MAC, in any of the usual notations
# (aa:bb:cc:dd:ee:ff, aa-bb-…, aabb.ccdd.eeff, aabbccddeeff).
_MAC_RE = re.compile(r"^(?:[0-9a-f]{2}([:-])(?:[0-9a-f]{2}\1){4}[0-9a-f]{2}|(?:[0-9a-f]{4}\.){2}[0-9a-f]{4}|[0-9a-f]{12})$", re.I)


def _normalize_mac(value):
    if not value or not _MAC_RE.match(value.strip()):
        return None
    hexdigits = re.sub(r"[^0-9a-f]", "", value.strip().lower())
    return ":".join(hexdigits[i:i + 2] for i in range(0, 12, 2))


# A WLC's node "port" is an AP radio: "<AP radio base MAC>.<slot>".
_RADIO_PORT_RE = re.compile(r"^((?:[0-9a-f]{2}:){5}[0-9a-f]{2})\.(\d+)$", re.I)


def ap_radio_links(ports):
    """Resolve WLC radio "ports" to the AP they belong to, via NetBox only.

    discobox no longer keeps these as pseudo-interfaces on the WLC (whose
    description used to carry the AP name); instead it puts the radio base
    MAC on the AP's first Dot11Radio<slot> interface. So: MAC -> AP device,
    and link the AP's Dot11Radio<slot> for that slot when it exists.

    Returns {port: {"url": radio iface URL or None, "ap_name", "ap_url"}}
    for the ports that resolved to exactly one device.
    """
    parsed = {}
    for port in ports:
        m = _RADIO_PORT_RE.match(port or "")
        if m:
            parsed[port] = (m.group(1).lower(), m.group(2))
    if not parsed or not NbMACAddress or not NbDevice:
        return {}
    devices_by_mac = {}
    for mac, device_id in NbMACAddress.objects.filter(
        mac_address__in={mac for mac, _ in parsed.values()}, interface__isnull=False,
    ).values_list("mac_address", "interface__device_id"):
        devices_by_mac.setdefault(str(mac).lower(), set()).add(device_id)
    ap_by_mac = {mac: ids.pop() for mac, ids in devices_by_mac.items() if len(ids) == 1}
    if not ap_by_mac:
        return {}
    devices = NbDevice.objects.in_bulk(set(ap_by_mac.values()))
    radio_urls = {}
    if NbInterface:
        for iface in NbInterface.objects.filter(
            device_id__in=devices, name__istartswith="dot11radio",
        ).only("device_id", "name"):
            radio_urls[(iface.device_id, iface.name.lower())] = iface.get_absolute_url()
    links = {}
    for port, (mac, slot) in parsed.items():
        ap = devices.get(ap_by_mac.get(mac))
        if ap:
            links[port] = {
                "url": radio_urls.get((ap.pk, f"dot11radio{slot}")),
                "ap_name": ap.name,
                "ap_url": ap.get_absolute_url(),
            }
    return links


def apply_ap_radio_links(rows):
    """For rows whose port didn't match a NetBox interface on the WLC itself,
    fill nb_interface_url / port_description / port_device_url from the AP.

    A row on an AP radio or with an SSID is wireless even when the
    wireless_ports call failed (e.g. a slow 9800 timing out), so is_wireless
    is filled in where still unknown; wireless_ports, when it answers, wins."""
    pending = [r for r in rows if r.get("port") and not r.get("nb_interface_url")]
    links = ap_radio_links({r["port"] for r in pending})
    for r in pending:
        link = links.get(r["port"])
        if link:
            r["nb_interface_url"] = link["url"]
            r["port_description"] = link["ap_name"]
            r["port_device_url"] = link["ap_url"]
    for r in rows:
        if r.get("is_wireless") is None and (r.get("port_device_url") or r.get("ssid")):
            r["is_wireless"] = True


def _device_ip(device):
    if not device.primary_ip4:
        return None
    return str(device.primary_ip4.address.ip)


def device_panel_context(device, user):
    """Everything in the device panel that needs Netdisco: stats, LENS Detail,
    neighbors. Served by LensDevicePanelDataView via htmx, so none of it runs
    while NetBox renders the device page itself."""
    config = settings.PLUGINS_CONFIG.get("netbox_lens", {})
    ip = _device_ip(device)
    backends = get_backends(config) if ip else []
    stats = None
    neighbors = []
    summaries = []
    if ip and backends:
        # Three independent Netdisco round trips — run them side by side.
        with ThreadPoolExecutor(max_workers=3) as executor:
            nodes_f = executor.submit(_device_nodes, backends, ip)
            neighbors_f = executor.submit(_fetch_neighbors, backends, ip)
            summaries_f = executor.submit(_device_summaries, backends, ip)
            nodes = [n for n in nodes_f.result() if n.get("active")]
            neighbors = _link_neighbors(neighbors_f.result(), device=device)
            summaries = summaries_f.result()
        stats = {
            "macs": len(nodes),
            "ports": len({n["port"] for n in nodes if n.get("port")}),
            "vlans": len({n["vlan"] for n in nodes if n.get("vlan") and n["vlan"] != "0"}),
            "neighbors": len(neighbors),
        }
        if device.role and device.role.slug == "lwapp-ctr":
            # Netdisco reports every AP radio as a port on the WLC (thousands
            # on a big one), so count from NetBox instead, as of discobox's
            # last WLC sync: APs point at their WLC via the controller CF, and
            # the WLC's own interfaces are its real ports (discobox never
            # syncs the radio pseudo-ports).
            cf_name = config.get("wlc_controller_cf") or "controller"
            stats["aps"] = NbDevice.objects.filter(**{f"custom_field_data__{cf_name}": device.pk}).count()
            stats["ports"] = NbInterface.objects.filter(device=device).count()
    found_labels = {label for label, _ in summaries}
    return {
        "lens_stats": stats,
        "lens_found": bool(summaries),
        "lens_device_ip": ip,
        "lens_summaries": summaries,
        "lens_backends": [b.label for b in backends],
        "lens_web_links": _device_web_links(backends, ip, found_labels) if ip else [],
        "lens_neighbors": neighbors,
        "lens_can_trigger": user.has_perm("netbox_lens.trigger_lens"),
        "lens_is_superuser": user.is_superuser,
        "lens_device_pk": device.pk,
        "lens_bossy_last_updated": device.cf.get("bossy_last_updated"),
        "lens_netdisco_last_update": device.cf.get("netdisco_last_update"),
        # set by discobox after each applied /rebuild (never on dry run or a plain sync)
        "lens_inventory_last_rebuild": device.cf.get("inventory_last_rebuild"),
        "lens_snmp_modulator_last_updated": device.cf.get("snmp_modulator_last_updated"),
    }


def device_panel_enabled(device, config):
    return bool(device.role) and device.role.slug in config.get("device_panel_roles", ())


class DeviceLensPanel(PluginTemplateExtension):
    models = ["dcim.device"]

    def right_page(self):
        if not self.context["request"].user.has_perm("netbox_lens.use_lens"):
            return ""
        device = self.context["object"]
        if not device_panel_enabled(device, settings.PLUGINS_CONFIG.get("netbox_lens", {})):
            return ""
        # No backend calls here: this renders the quick-search links plus
        # placeholders, and device_panel_data fills in the rest via htmx.
        return self.render("netbox_lens/device_nodes_panel.html", extra_context={
            "lens_device_name": device.name,
            "lens_device_ip": _device_ip(device),
            "lens_device_pk": device.pk,
        })


def interface_nodes_context(iface):
    """Active nodes on one interface — the Netdisco-backed part of the
    interface panel, served by LensInterfaceNodesDataView via htmx."""
    ip = _device_ip(iface.device)
    backends = get_backends(settings.PLUGINS_CONFIG.get("netbox_lens", {})) if ip else []
    nodes = []
    if backends:
        nodes = [n for n in _device_nodes(backends, ip, port=iface.name) if n.get("active")]
    return {"lens_nodes": nodes, "lens_device_ip": ip, "lens_port": iface.name}


class InterfaceLensPanel(PluginTemplateExtension):
    models = ["dcim.interface"]

    def full_width_page(self):
        if not self.context["request"].user.has_perm("netbox_lens.use_lens"):
            return ""
        iface = self.context["object"]
        # get_backends only reads config, no network — cheap enough to decide
        # here whether there's anything to load at all.
        if not _device_ip(iface.device) or not get_backends(settings.PLUGINS_CONFIG.get("netbox_lens", {})):
            return ""
        return self.render("netbox_lens/interface_nodes_panel.html", extra_context={
            "lens_interface_pk": iface.pk,
            "lens_port": iface.name,
        })


template_extensions = [DeviceLensPanel, InterfaceLensPanel]
