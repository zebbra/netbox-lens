from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

from .template_content import _device_nodes

try:
    from dcim.models import Device as NbDevice
except ImportError:
    NbDevice = None

MAX_PORT_MATCHES = 50
MAX_DEVICE_MATCHES = 20
MAX_ROWS = 500


def _port_targets(backends, device_query=None, interface_query=None, vlan_query=None):
    """Resolve (device_ip, device_name, port) targets via Netdisco's global port search,
    intersecting interface and VLAN matches when both are given."""
    def _matches(query):
        found = []
        for b in backends:
            found.extend(b.search_ports(query, partial=True))
        return found

    interface_matches = _matches(interface_query) if interface_query else None
    vlan_matches = _matches(vlan_query) if vlan_query else None

    if interface_matches is not None and vlan_matches is not None:
        vlan_keys = {(m.get("ip"), m.get("port")) for m in vlan_matches}
        matches = [m for m in interface_matches if (m.get("ip"), m.get("port")) in vlan_keys]
    else:
        matches = interface_matches if interface_matches is not None else vlan_matches

    if device_query:
        q = device_query.lower()
        matches = [
            m for m in matches
            if q in ((m.get("device") or {}).get("dns") or "").lower()
            or q in ((m.get("device") or {}).get("name") or "").lower()
        ]

    truncated = len(matches) > MAX_PORT_MATCHES
    matches = matches[:MAX_PORT_MATCHES]
    targets = [
        (m.get("ip"), (m.get("device") or {}).get("dns") or (m.get("device") or {}).get("name"), m.get("port"))
        for m in matches if m.get("ip")
    ]
    return targets, truncated


def _device_targets(device_query):
    if not NbDevice:
        return [], False
    qs = NbDevice.objects.filter(name__icontains=device_query).select_related("primary_ip4")
    devices = list(qs[:MAX_DEVICE_MATCHES + 1])
    truncated = len(devices) > MAX_DEVICE_MATCHES
    devices = devices[:MAX_DEVICE_MATCHES]
    targets = [(str(d.primary_ip4.address.ip), d.name, None) for d in devices if d.primary_ip4]
    return targets, truncated


def _fetch_nodes_for_targets(backends, targets, since=None, until=None, require_active=True):
    nodes = []
    with ThreadPoolExecutor() as executor:
        futures = {
            executor.submit(_device_nodes, backends, ip, port, since, until): (ip, name)
            for ip, name, port in targets
        }
        for future in as_completed(futures):
            ip, name = futures[future]
            for n in future.result():
                if require_active and not n.get("active"):
                    continue
                n = dict(n)
                n["_device_ip"] = ip
                n["_device_name"] = name
                nodes.append(n)
    return nodes


def _fetch_sightings(backends, query, partial=False, since=None, until=None, require_active=True):
    nodes = []
    for b in backends:
        nodes.extend(
            n for n in b.node_sightings(query, partial=partial, since=since, until=until)
            if not require_active or n.get("active")
        )
    return nodes


def _apply_post_filters(nodes, device_query=None, interface_query=None, vlan_query=None):
    if device_query:
        q = device_query.lower()
        nodes = [n for n in nodes if q in (n.get("_device_name") or "").lower()]
    if interface_query:
        q = interface_query.lower()
        nodes = [n for n in nodes if q in (n.get("port") or "").lower()]
    if vlan_query:
        nodes = [n for n in nodes if str(n.get("vlan")) == str(vlan_query)]
    return nodes


def _apply_date_filter(nodes, date_from=None, date_to=None):
    """Safety net: bound results by time_last (or time_first as fallback) regardless
    of whether the backend honored the daterange server-side."""
    if not date_from and not date_to:
        return nodes
    filtered = []
    for n in nodes:
        stamp = n.get("time_last") or n.get("time_first")
        if not stamp:
            continue
        try:
            day = datetime.fromisoformat(stamp).date()
        except ValueError:
            continue
        if date_from and day < date_from:
            continue
        if date_to and day > date_to:
            continue
        filtered.append(n)
    return filtered


def build_mac_history(
    backends, device_ip=None, device_query=None, interface_query=None,
    vlan_query=None, mac_query=None, client_query=None, max_rows=MAX_ROWS,
    date_from=None, date_to=None,
):
    """
    Correlate port/MAC sightings with ARP-resolved client IP/DNS.

    device_ip scopes to exactly one device (the device-tab case). Otherwise, mac_query
    or client_query drive a global sightings lookup (with device/interface/vlan applied
    as post-filters); failing that, interface_query/vlan_query drive a global port search
    (optionally narrowed by device_query); failing that, device_query alone resolves via
    NetBox's own device list.

    date_from/date_to (date objects) bound results by last-seen date. When given, the
    active-only requirement is relaxed so archived/historical nodes are considered too,
    then _apply_date_filter narrows to the requested window client-side.

    Returns (rows, total_count, truncated, port_match_truncated).
    """
    port_match_truncated = False
    since = date_from.isoformat() if date_from else None
    until = date_to.isoformat() if date_to else None
    require_active = not (date_from or date_to)

    if device_ip:
        nodes = _fetch_nodes_for_targets(
            backends, [(device_ip, None, None)], since=since, until=until, require_active=require_active,
        )
    elif mac_query:
        nodes = _fetch_sightings(
            backends, mac_query, partial=False, since=since, until=until, require_active=require_active,
        )
        nodes = _apply_post_filters(nodes, device_query, interface_query, vlan_query)
    elif client_query:
        # Node search only populates "sightings" (port-level) for MAC-shaped
        # queries — an IP/hostname query only returns ARP-level "macs" data with
        # no port. Resolve to MAC(s) first, then fetch sightings per MAC.
        macs_found = set()
        for b in backends:
            macs_found.update(b.find_macs(client_query, partial=True))
        nodes = []
        for mac in macs_found:
            nodes.extend(_fetch_sightings(
                backends, mac, partial=False, since=since, until=until, require_active=require_active,
            ))
        nodes = _apply_post_filters(nodes, device_query, interface_query, vlan_query)
    elif interface_query or vlan_query:
        targets, port_match_truncated = _port_targets(backends, device_query, interface_query, vlan_query)
        nodes = (
            _fetch_nodes_for_targets(backends, targets, since=since, until=until, require_active=require_active)
            if targets else []
        )
        # A matched port (e.g. a trunk) can carry more VLANs than the one searched —
        # the port-level match doesn't guarantee every node on it matches too.
        nodes = _apply_post_filters(nodes, vlan_query=vlan_query)
    elif device_query:
        targets, _ = _device_targets(device_query)
        nodes = (
            _fetch_nodes_for_targets(backends, targets, since=since, until=until, require_active=require_active)
            if targets else []
        )
    else:
        return [], 0, False, False

    nodes = _apply_date_filter(nodes, date_from, date_to)

    macs = {n["mac"] for n in nodes if n.get("mac")}
    client_map = {}
    if macs:
        with ThreadPoolExecutor() as executor:
            futures = {
                executor.submit(_resolve_mac_across_backends, backends, mac): mac
                for mac in macs
            }
            for future in as_completed(futures):
                mac = futures[future]
                result = future.result()
                if result:
                    client_map[mac] = result

    rows = []
    for n in nodes:
        client = client_map.get(n.get("mac")) or {}
        rows.append({
            "device_ip": n.get("_device_ip"),
            "device_name": n.get("_device_name"),
            "port": n.get("port"),
            "mac": n.get("mac"),
            "vlan": n.get("vlan"),
            "client_ip": client.get("ip"),
            "client_name": client.get("dns"),
            "time_first": n.get("time_first"),
            "time_last": n.get("time_last"),
        })

    rows.sort(key=lambda r: r.get("time_last") or "", reverse=True)
    total = len(rows)
    truncated = total > max_rows
    return rows[:max_rows], total, truncated, port_match_truncated


def _resolve_mac_across_backends(backends, mac):
    for b in backends:
        result = b.resolve_mac(mac)
        if result:
            return result
    return None
