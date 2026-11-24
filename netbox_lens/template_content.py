import operator
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
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
    from ipam.models import IPAddress as NbIPAddress
except ImportError:
    NbIPAddress = None

try:
    from dcim.models import MACAddress as NbMACAddress  # NetBox >= 4.2
except ImportError:
    NbMACAddress = None


def _device_nodes(backends, device_ip, port=None, since=None, until=None, port_context=False):
    nodes = []
    with ThreadPoolExecutor() as executor:
        futures = [executor.submit(b.device_nodes, device_ip, since, until, port_context) for b in backends]
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
                "ap_id": ap.pk,
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
            r["port_device_id"] = link["ap_id"]
    for r in rows:
        if r.get("is_wireless") is None and (r.get("port_device_url") or r.get("ssid")):
            r["is_wireless"] = True
    _apply_ap_nodes(rows)


# A switch sighting and a WLC sighting count as the same visit when their
# first/last-seen spans overlap or are at most this far apart.
_AP_UPLINK_SLACK = timedelta(hours=1)


def _sighting_span(row):
    def parse(v):
        if isinstance(v, datetime):
            return v.replace(tzinfo=None)
        try:
            return datetime.fromisoformat(str(v)[:19])
        except ValueError:
            return None
    return parse(row.get("time_first")), parse(row.get("time_last"))


def _spans_meet(a, b):
    (a0, a1), (b0, b1) = _sighting_span(a), _sighting_span(b)
    if None in (a0, a1, b0, b1):
        return False
    return a0 - _AP_UPLINK_SLACK <= b1 and b0 - _AP_UPLINK_SLACK <= a1


def merge_ap_uplinks(rows):
    """A wireless client shows up twice: on the WLC (radio port resolved to
    its AP by apply_ap_radio_links) and on the switch port the AP hangs off.
    Fold that switch sighting into the WLC row (via_switch, via_port), so the
    client reads as one row: WLC, AP, via switch port. The switch port counts
    as the AP's when its CDP/LLDP neighbor IP is on the AP in NetBox, or its
    description names the AP; same MAC, overlapping time. In place."""
    wifi = [r for r in rows if r.get("port_device_id")]
    wired = [r for r in rows if not r.get("port_device_id") and r.get("is_wireless") is not True]
    if not wifi or not wired:
        return
    ap_by_ip = {}
    ips = {r["port_remote_ip"] for r in wired if r.get("port_remote_ip")}
    if ips and NbIPAddress:
        q = reduce(operator.or_, (Q(address__net_host=ip) for ip in ips))
        for address, device_id in NbIPAddress.objects.filter(q, interface__isnull=False).values_list(
                "address", "interface__device_id"):
            ap_by_ip[str(address.ip)] = device_id
    merged = set()
    for w in wifi:
        short = (w.get("port_description") or "").split(".")[0].lower()
        name_re = re.compile(rf"(?<![\w-]){re.escape(short)}(?![\w-])") if short else None
        for c in wired:
            if id(c) in merged or (c.get("mac") or "").lower() != (w.get("mac") or "").lower():
                continue
            by_ip = ap_by_ip.get(c.get("port_remote_ip")) == w["port_device_id"]
            by_name = bool(name_re and name_re.search((c.get("port_description") or "").lower()))
            if (by_ip or by_name) and _spans_meet(w, c):
                # sightings carry device{name}/switch, MAC History rows device_name/device_ip
                w["via_switch"] = ((c.get("device") or {}).get("name") or c.get("device_name")
                                   or c.get("switch") or c.get("device_ip"))
                w["via_switch_url"] = c.get("nb_device_url")
                w["via_port"] = c.get("port")
                w["via_port_url"] = c.get("nb_interface_url")
                # the WLC never reports the client's VLAN (stored as 0), the switch port does
                if str(w.get("vlan") or "0") == "0" and str(c.get("vlan") or "0") != "0":
                    w["vlan"] = c["vlan"]
                    w["vlan_from_switch"] = True
                merged.add(id(c))
                break
    rows[:] = [r for r in rows if id(r) not in merged]


def _apply_ap_nodes(rows):
    """Mark rows whose MAC is an AP itself (its wired uplink, e.g. the
    GigabitEthernet0 MAC discobox syncs), so the AP reads as an AP, not as
    just another wired client."""
    if not NbMACAddress or not NbDevice:
        return
    roles = settings.PLUGINS_CONFIG.get("netbox_lens", {}).get("ap_roles") or ()
    macs = {m for m in (_normalize_mac(r.get("mac")) for r in rows) if m}
    if not roles or not macs:
        return
    devices_by_mac = {}
    for mac, device_id in NbMACAddress.objects.filter(
        mac_address__in=macs, interface__device__role__slug__in=roles,
    ).values_list("mac_address", "interface__device_id"):
        devices_by_mac.setdefault(str(mac).lower(), set()).add(device_id)
    ap_by_mac = {mac: ids.pop() for mac, ids in devices_by_mac.items() if len(ids) == 1}
    devices = NbDevice.objects.in_bulk(set(ap_by_mac.values()))
    for r in rows:
        ap = devices.get(ap_by_mac.get(_normalize_mac(r.get("mac"))))
        if ap:
            r["node_ap_name"] = ap.name
            r["node_ap_url"] = ap.get_absolute_url()


def apply_transit_ports(rows):
    """Set transit_via on rows seen on a port toward another network device
    (an uplink, or a port-channel whose member leads to one), from Netdisco's
    port_context fields: the port (or its LAG's member) has a neighbor
    Netdisco discovered as a device, and it isn't a phone or AP. The MAC only
    passes through there. The neighbor is named from NetBox when its IP is
    there; rows without port_context stay edge."""
    if not NbIPAddress:
        return
    remote = {}
    for r in rows:
        ip = r.get("port_remote_ip") or r.get("lag_remote_ip")
        if ip and r.get("neighbor_is_device") and not (r.get("port_remote_is_wap") or r.get("port_remote_is_phone")):
            remote[id(r)] = ip
    if not remote:
        return
    q = reduce(operator.or_, (Q(address__net_host=ip) for ip in set(remote.values())))
    names = {
        str(address.ip): name
        for address, name in NbIPAddress.objects.filter(q, interface__isnull=False)
        .values_list("address", "interface__device__name")
    }
    for r in rows:
        ip = remote.get(id(r))
        if ip:
            r["transit_via"] = names.get(ip, ip)


def _device_ip(device):
    if not device.primary_ip4:
        return None
    return str(device.primary_ip4.address.ip)


def device_panel_context(device, user):
    """Everything in the device panel that needs Netdisco: WLC stats and LENS
    Detail. Served by LensDevicePanelDataView via htmx, so none of it runs
    while NetBox renders the device page itself."""
    config = settings.PLUGINS_CONFIG.get("netbox_lens", {})
    ip = _device_ip(device)
    backends = get_backends(config) if ip else []
    stats = None
    summaries = []
    if ip and backends:
        summaries = _device_summaries(backends, ip)
        if _is_wlc(device):
            # Netdisco reports every AP radio as a port on the WLC (thousands
            # on a big one), so count from NetBox instead, as of discobox's
            # last WLC sync: APs point at their WLC via the controller CF, and
            # the WLC's own interfaces are its real ports (discobox never
            # syncs the radio pseudo-ports).
            cf_name = config.get("wlc_controller_cf") or "controller"
            stats = {
                "aps": NbDevice.objects.filter(**{f"custom_field_data__{cf_name}": device.pk}).count(),
                "ports": NbInterface.objects.filter(device=device).count(),
            }
    found_labels = {label for label, _ in summaries}
    holder = None if summaries or not backends else _ip_holder(backends, ip)
    return {
        "lens_enabled": bool(ip and backends),
        "lens_stats": stats,
        "lens_found": bool(summaries),
        "lens_ip_holder": holder,
        "lens_ip_conflict": bool(holder and not holder["nb_url"]),
        "lens_device_ip": ip,
        "lens_summaries": summaries,
        "lens_backends": [b.label for b in backends],
        "lens_web_links": _device_web_links(backends, ip, found_labels) if ip else [],
        "lens_can_trigger": user.has_perm("netbox_lens.trigger_lens"),
        "lens_is_superuser": user.is_superuser,
        "lens_device_pk": device.pk,
        "lens_netdisco_last_update": device.cf.get("netdisco_last_update"),
        # set by discobox after each applied /rebuild (never on dry run or a plain sync)
        "lens_inventory_last_rebuild": device.cf.get("inventory_last_rebuild"),
    }


def _is_wlc(device):
    return bool(device.role and device.role.slug == "lwapp-ctr")


def _netbox_device_for(holder):
    """The NetBox device a Netdisco device is, by exact name, short name or
    serial (discobox's rule). A cluster VIP is held by a member that is in
    NetBox; a stale holder that is gone from NetBox is a conflict."""
    if not NbDevice:
        return None
    name = holder.get("name") or ""
    short = name.split(".")[0]
    q = Q(name__iexact=name) | Q(name__iexact=short) | Q(name__istartswith=f"{short}.")
    if holder.get("serial"):
        q |= Q(serial__iexact=holder["serial"])
    return NbDevice.objects.filter(q).first() if name else None


def _ip_holder(backends, ip):
    """Another Netdisco device holding ip as an alias, with "nb_device" set
    when that device is in NetBox (a cluster member: discobox syncs through
    it on purpose, nothing to block)."""
    for b in backends:
        holder = b.ip_holder(ip)
        if holder:
            nb = _netbox_device_for(holder)
            return {**holder, "backend": b.label,
                    "nb_name": nb.name if nb else None,
                    "nb_url": nb.get_absolute_url() if nb else None}
    return None


def device_ip_conflict(device):
    """The Netdisco device that holds this device's primary IP as an alias,
    when Netdisco has no device at that IP. Sync, rebuild and discover would
    all land on that holder, so the device page blocks them. None otherwise."""
    ip = _device_ip(device)
    backends = get_backends(settings.PLUGINS_CONFIG.get("netbox_lens", {})) if ip else []
    if not backends or _device_summaries(backends, ip):
        return None
    holder = _ip_holder(backends, ip)
    return holder if holder and not holder["nb_url"] else None


def device_neighbors_context(device):
    """The device's LLDP/CDP neighbors from Netdisco, linked to NetBox, for
    the Neighbors tab. One /ports call, only when the tab is opened."""
    ip = _device_ip(device)
    backends = get_backends(settings.PLUGINS_CONFIG.get("netbox_lens", {})) if ip else []
    return {
        "lens_device_ip": ip,
        "lens_enabled": bool(backends),
        "lens_neighbors": _link_neighbors(_fetch_neighbors(backends, ip), device=device) if backends else [],
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
            "lens_can_trigger": self.context["request"].user.has_perm("netbox_lens.trigger_lens"),
        })


def interface_port_context(iface):
    """Netdisco's view of one port (state, link change, last MAC, neighbor,
    PoE, NAC) — the interface panel's "Port info", loaded on request."""
    ip = _device_ip(iface.device)
    backends = get_backends(settings.PLUGINS_CONFIG.get("netbox_lens", {})) if ip else []
    info = backends[0].port_info(ip, iface.name) if backends else None
    neighbor = (info or {}).get("neighbor")
    if neighbor and neighbor.get("ip") and NbDevice is not None:
        # the neighbor as a NetBox device (and port), where it is one
        device = NbDevice.objects.filter(primary_ip4__address__net_host=neighbor["ip"]).first()
        if device:
            neighbor["nb_device_url"] = device.get_absolute_url()
            neighbor["nb_device"] = device.name
            remote = NbInterface.objects.filter(device=device, name=neighbor.get("port")).first() if neighbor.get("port") else None
            neighbor["nb_port_url"] = remote.get_absolute_url() if remote else None
    return {"lens_info": info, "lens_device_ip": ip, "lens_port": iface.name,
            "lens_device_name": getattr(iface.device, "name", None)}


def interface_nodes_context(iface, history=False):
    """Nodes on one interface — the interface panel's "MACs", loaded on
    request by LensInterfaceNodesDataView. history: every MAC Netdisco still
    keeps for the port (also long gone ones), newest first."""
    ip = _device_ip(iface.device)
    backends = get_backends(settings.PLUGINS_CONFIG.get("netbox_lens", {})) if ip else []
    nodes = []
    truncated = False
    if backends and history:
        nodes = [dict(n) for n in _device_nodes(backends, ip, port=iface.name, since=HISTORY_SINCE)]
        nodes.sort(key=lambda n: n.get("time_last") or "", reverse=True)
        truncated = len(nodes) > MAX_HISTORY_NODES
        nodes = nodes[:MAX_HISTORY_NODES]
    elif backends:
        nodes = [dict(n) for n in _device_nodes(backends, ip, port=iface.name) if n.get("active")]
    if backends:
        # client IP/name from ARP, where some router has resolved the MAC
        macs = list(dict.fromkeys(n["mac"] for n in nodes if n.get("mac")))[:MAX_RESOLVED_MACS]  # newest first in history
        with ThreadPoolExecutor() as executor:
            clients = dict(zip(macs, executor.map(lambda mac: _resolve_mac(backends, mac), macs)))
        for n in nodes:
            client = clients.get(n.get("mac")) or {}
            n["client_ip"] = client.get("ip")
            n["client_name"] = client.get("dns")
    nac = _nac(backends[0].port_pae(ip, iface.name)) if backends else None
    for n in nodes:
        n["nac_session"] = _is_nac_session(nac, n.get("mac"))
    return {
        "lens_nac": nac,
        "lens_nodes": nodes, "lens_device_ip": ip, "lens_port": iface.name,
        "lens_device_name": getattr(iface.device, "name", None),
        "lens_history": history, "lens_truncated": truncated, "lens_max": MAX_HISTORY_NODES,
    }


# NAC (802.1X/MAB) needs one Netdisco call per switch port, so only for the
# few active wired ports of a lookup.
MAX_NAC_PORTS = 5


def _nac(pae):
    """port_pae() result, or None when the port has no 802.1X/MAB at all."""
    return pae if pae and (pae.get("authconfig_state") or pae.get("port_control")) else None


def _is_nac_session(nac, mac):
    """Whether this MAC holds the port's NAC session: it sent the last EAPOL
    frame, or MAB let it in (the MAB user is the MAC itself)."""
    m = _normalize_mac(mac)
    if not m or not nac:
        return False
    return m == _normalize_mac(nac.get("last_eapol_source")) or bool(
        nac.get("mab") and m == _normalize_mac(nac.get("user")))


def apply_port_nac(rows, backend):
    """Attach the switch port's NAC state (nac, nac_session) to the active
    wired, non-transit sightings of a lookup. In place."""
    def wired(r):
        return (r.get("active") and r.get("is_wireless") is not True and not r.get("port_device_url")
                and not r.get("transit_via") and r.get("switch") and r.get("port"))
    targets = list(dict.fromkeys((r["switch"], r["port"]) for r in rows if wired(r)))[:MAX_NAC_PORTS]
    if not targets:
        return
    with ThreadPoolExecutor(max_workers=MAX_NAC_PORTS) as executor:
        nacs = dict(zip(targets, executor.map(lambda t: _nac(backend.port_pae(*t)), targets)))
    for r in rows:
        nac = nacs.get((r.get("switch"), r.get("port"))) if wired(r) else None
        if nac:
            r["nac"] = nac
            r["nac_session"] = _is_nac_session(nac, r.get("mac"))


# Port history: everything Netdisco keeps (its node expiry decides how far back).
HISTORY_SINCE = "2000-01-01"
MAX_HISTORY_NODES = 500

# An access port has a handful of MACs; cap the per-MAC ARP lookups for
# the odd port with hundreds (an uplink).
MAX_RESOLVED_MACS = 50


def _resolve_mac(backends, mac):
    for b in backends:
        result = b.resolve_mac(mac)
        if result:
            return result
    return None


class InterfaceLensPanel(PluginTemplateExtension):
    models = ["dcim.interface"]

    def right_page(self):
        iface = self.context["object"]
        if not self._enabled(iface):
            return ""
        return self.render("netbox_lens/interface_port_panel.html", extra_context=self._ctx(iface))

    def _enabled(self, iface):
        # get_backends only reads config, no network — cheap enough to decide
        # here whether there's anything to load at all.
        return (self.context["request"].user.has_perm("netbox_lens.use_lens")
                and _device_ip(iface.device)
                and get_backends(settings.PLUGINS_CONFIG.get("netbox_lens", {})))

    @staticmethod
    def _ctx(iface):
        return {"lens_interface_pk": iface.pk, "lens_port": iface.name}


class DeviceListLensButtons(PluginTemplateExtension):
    models = ["dcim.device"]

    def list_buttons(self):
        request = self.context["request"]
        if not request.user.has_perm("netbox_lens.trigger_lens"):
            return ""
        # hand the list's current filter over to the bulk page
        return self.render("netbox_lens/device_list_buttons.html", extra_context={
            "lens_filter_qs": request.GET.urlencode(),
        })


template_extensions = [DeviceLensPanel, InterfaceLensPanel, DeviceListLensButtons]
