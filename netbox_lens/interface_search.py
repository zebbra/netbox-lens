from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import quote

try:
    from dcim.models import Interface as NbInterface
except ImportError:
    NbInterface = None

MAX_ROWS = 200
MAX_SCAN = 2000

# Ordered longest-name-first so no shorter name accidentally shadows a longer one.
IFNAME_ABBREVIATIONS = [
    ("TenGigabitEthernet", "Te"),
    ("TwentyFiveGigE", "Twe"),
    ("FortyGigabitEthernet", "Fo"),
    ("HundredGigE", "Hu"),
    ("GigabitEthernet", "Gi"),
    ("FastEthernet", "Fa"),
    ("Port-channel", "Po"),
    ("Loopback", "Lo"),
    ("Ethernet", "Et"),
    ("Vlan", "Vl"),
]


def _abbreviate_ifname(name):
    for full, short in IFNAME_ABBREVIATIONS:
        if name.startswith(full):
            return short + name[len(full):]
    return name


def _format_speed(kbps):
    if not kbps:
        return None
    if kbps % 1_000_000 == 0:
        return f"{kbps // 1_000_000} GBit/s"
    if kbps >= 1_000_000:
        return f"{kbps / 1_000_000:.1f} GBit/s"
    if kbps % 1_000 == 0:
        return f"{kbps // 1_000} MBit/s"
    return f"{kbps / 1_000:.1f} MBit/s"


def grafana_url(template, device_name, interface_name):
    if not template or not device_name or not interface_name:
        return None
    return template.format(
        instance=quote(device_name, safe=""),
        ifname=quote(_abbreviate_ifname(interface_name), safe=""),
    )


def build_interface_list(
    device_query=None, interface_query=None, description_query=None,
    vlan_query=None, speed_query=None, managed_query=None, admin_query=None,
    max_rows=MAX_ROWS, grafana_template=None,
):
    """
    Filterable interface inventory sourced directly from NetBox's own synced
    Interface objects (no live Netdisco calls needed).

    speed, managed and vlan are matched against their formatted/CF/NetBox
    display values in Python after the DB-level filters narrow the candidate
    set, rather than as DB filters — vlan in particular is rarely populated in
    NetBox itself, so this same post-filter can be re-applied by the caller
    once a live refresh has populated real VLAN numbers from Netdisco.

    Returns (rows, total_count, truncated, scan_truncated).
    """
    if not NbInterface:
        return [], 0, False, False

    qs = NbInterface.objects.select_related("device", "device__primary_ip4", "untagged_vlan")
    if device_query:
        qs = qs.filter(device__name__icontains=device_query)
    if interface_query:
        qs = qs.filter(name__icontains=interface_query)
    if description_query:
        qs = qs.filter(description__icontains=description_query)
    if admin_query == "up":
        qs = qs.filter(enabled=True)
    elif admin_query == "down":
        qs = qs.filter(enabled=False)
    qs = qs.order_by("device__name", "name")

    interfaces = list(qs[:MAX_SCAN + 1])
    scan_truncated = len(interfaces) > MAX_SCAN
    interfaces = interfaces[:MAX_SCAN]

    rows = []
    for iface in interfaces:
        managed = iface.cf.get("interface_severity")
        if managed_query and managed_query.lower() not in (managed or "").lower():
            continue
        speed = _format_speed(iface.speed)
        if speed_query and speed_query.lower() not in (speed or "").lower():
            continue
        vlan = iface.untagged_vlan.vid if iface.untagged_vlan else None
        if vlan_query and str(vlan) != str(vlan_query):
            continue
        rows.append({
            "device_name": iface.device.name,
            "device_ip": str(iface.device.primary_ip4.address.ip) if iface.device.primary_ip4 else None,
            "nb_device_url": iface.device.get_absolute_url(),
            "interface_name": iface.name,
            "nb_interface_url": iface.get_absolute_url(),
            "description": iface.description,
            "vlan": vlan,
            "speed": speed,
            "managed": managed,
            "admin": "up" if iface.enabled else "down",
            "oper": None,
            "type": iface.type,
            "poe_type": iface.poe_type,
            "updated": iface.last_updated.isoformat() if iface.last_updated else None,
            "grafana_url": grafana_url(grafana_template, iface.device.name, iface.name),
        })

    total = len(rows)
    truncated = total > max_rows
    return rows[:max_rows], total, truncated, scan_truncated


def apply_live_status(rows, backends, vlan_query=None):
    """Overlay fresh admin/oper/vlan onto rows from Netdisco, one call per
    distinct device among the (already-filtered) rows given.

    vlan_query re-applies the VLAN post-filter now that real values have
    landed — NetBox rarely has VLAN set on its own, so the initial filter
    in build_interface_list() may have had nothing to match against yet.

    Returns the (possibly narrowed) rows list.
    """
    device_ips = {r["device_ip"] for r in rows if r.get("device_ip")}
    if not device_ips or not backends:
        return rows

    ports_by_device = {}
    with ThreadPoolExecutor() as executor:
        futures = {}
        for ip in device_ips:
            for b in backends:
                futures[executor.submit(b.device_ports, ip)] = ip
        for future in as_completed(futures):
            ip = futures[future]
            result = future.result()
            if result:
                ports_by_device.setdefault(ip, {}).update({p["port"]: p for p in result if p.get("port")})

    for row in rows:
        port_map = ports_by_device.get(row.get("device_ip"))
        if not port_map:
            continue
        live = port_map.get(row["interface_name"])
        if not live:
            continue
        if live.get("up_admin"):
            row["admin"] = live["up_admin"]
        if live.get("up"):
            row["oper"] = live["up"]
        if live.get("vlan"):
            row["vlan"] = live["vlan"]

    if vlan_query:
        rows = [r for r in rows if str(r.get("vlan")) == str(vlan_query)]
    return rows
