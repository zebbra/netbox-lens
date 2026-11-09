"""Down Ports: access ports free for reuse, from Netdisco's portfree report.

"Free" is MAC-based, not ifLastChange-based: a port qualifies when no MAC was
seen on it for N days (or never). lastchange resets on every reboot, the MAC
history doesn't. With oper=up the same report lists ports that are up but
silent, e.g. a client failing 802.1X (closed mode never learns its MAC).

Devices Netdisco never collects MACs on (deliberately or not) fall back to the
link change; the report decides which (free_since_basis), see _age().
"""
import operator
from functools import reduce

from django.db.models import Q
from django.utils import timezone

from .interface_search import _format_speed, _natural_sort_key, grafana_url
from .templatetags.lens_filters import _parse

try:
    from dcim.models import Device as NbDevice
    from dcim.models import Interface as NbInterface
except ImportError:
    NbDevice = None
    NbInterface = None

MAX_ROWS = 2000

def _days_since(value):
    dt, _ = _parse(value)
    return (timezone.now() - dt).days if dt else None


def _age(p):
    """(basis, days, clock) for how long a port has been unused, from the
    report's free_since / free_since_basis:

    "mac":        MAC history (days None = no MAC ever known); clock = last macsuck
    "lastchange": no MAC data on the device, port down since its link change
    "boot":       same, but lastchange is only the reboot: days is a lower bound
    None:         no MAC data and no usable link change (missing, or port up)
    """
    basis = p.get("free_since_basis")
    clock = p.get("last_macsuck") if basis == "mac" else p.get("last_discover") if basis else None
    return basis, _days_since(p.get("free_since")), clock


def _netbox_maps(ips):
    """{ip: Device} and {(device_id, name): Interface} for the report's devices."""
    if not NbDevice or not ips:
        return {}, {}
    q = reduce(operator.or_, (Q(primary_ip4__address__net_host=ip) for ip in ips))
    devices = {
        str(d.primary_ip4.address.ip): d
        for d in NbDevice.objects.filter(q).select_related("primary_ip4")
    }
    interfaces = {}
    if NbInterface and devices:
        qs = NbInterface.objects.filter(device__in=devices.values())
        for iface in qs.only("id", "device_id", "name", "speed", "custom_field_data"):
            interfaces[(iface.device_id, iface.name)] = iface
    return devices, interfaces


def _contains(needle, *haystacks):
    needle = needle.lower()
    return any(needle in (h or "").lower() for h in haystacks)


def build_down_ports(backend, min_days=30, oper="down", admin="any", device_query=None,
                     interface_query=None, description_query=None, vlan_query=None,
                     grafana_template=None):
    """Returns (rows, total, truncated, error)."""
    data, error = backend.free_ports(age_num=min_days, up=oper, up_admin=admin)
    if data is None:
        return [], 0, False, error

    devices, interfaces = _netbox_maps({p["ip"] for p in data if p.get("ip")})
    rows = []
    for p in data:
        device = devices.get(p.get("ip"))
        iface = interfaces.get((device.pk, p.get("port"))) if device else None
        device_name = device.name if device else (p.get("dns") or p.get("device_name") or p.get("ip"))
        if device_query and not _contains(device_query, device_name, p.get("dns"), p.get("device_name"), p.get("ip")):
            continue
        if interface_query and not _contains(interface_query, p.get("port")):
            continue
        if description_query and not _contains(description_query, p.get("name")):
            continue
        if vlan_query and str(p.get("vlan") or "") != str(vlan_query):
            continue
        basis, age_days, age_clock = _age(p)
        # The report applies min_days to the MAC age only; link-based ages are
        # checked here. Rows without any usable age stay: unknown, not free.
        if basis in ("lastchange", "boot") and (age_days is None or age_days < min_days):
            continue
        speed = p.get("speed")
        rows.append({
            "device_name": device_name,
            "device_ip": p.get("ip"),
            "nb_device_url": device.get_absolute_url() if device else None,
            "interface_name": p.get("port"),
            "nb_interface_url": iface.get_absolute_url() if iface else None,
            "description": p.get("name"),
            "vlan": p.get("vlan"),
            # Netdisco reports speed as text ("1 Gbps"); NetBox's own value
            # (kbps) is more consistent with the rest of LENS when present.
            "speed": _format_speed(iface.speed) if iface and iface.speed else speed,
            "managed": iface.cf.get("interface_severity") if iface else None,
            "admin": p.get("up_admin"),
            "oper": p.get("up"),
            "last_mac": p.get("last_mac"),
            "last_mac_seen": p.get("last_mac_seen"),
            "days_since_mac": p.get("days_since_mac"),
            # portfree has no explicit flag: no last_mac_seen means no MAC known
            "no_node": p.get("last_mac_seen") is None,
            "last_macsuck": p.get("last_macsuck"),
            "age_basis": basis,
            "age_days": age_days,
            "age_clock": age_clock,
            "last_discover": p.get("last_discover"),
            "free_since": p.get("free_since"),
            "lastchange_stamp": p.get("lastchange_stamp"),
            "device_boot": p.get("device_boot"),
            "grafana_url": grafana_url(grafana_template, device_name, p.get("port")),
        })

    rows.sort(key=lambda r: ((r["device_name"] or "").lower(), _natural_sort_key(r["interface_name"])))
    total = len(rows)
    return rows[:MAX_ROWS], total, total > MAX_ROWS, None
