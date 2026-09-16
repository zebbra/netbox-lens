import re

from .mac_history import _apply_date_filter

MAX_ROWS = 500

# Netdisco's OUI table describes locally-administered/randomized MAC ranges
# with entries like "randomized address [0-f][26ae]:xx:xx:xx" instead of a
# real vendor name — that regex-looking range spec means nothing to a user,
# so it's collapsed to a plain "Randomized" label.
_RANDOMIZED_VENDOR_RE = re.compile(r"^randomized address\b", re.IGNORECASE)


def _clean_vendor(vendor):
    if vendor and _RANDOMIZED_VENDOR_RE.match(vendor):
        return "Randomized"
    return vendor


def build_arp_history(
    backends, mac_query=None, client_query=None, device_query=None,
    date_from=None, date_to=None, max_rows=MAX_ROWS,
):
    """
    Look up ARP-level (NodeIp) records: client IP/MAC pairs as last seen on a router,
    without the port/VLAN dimension that MAC History carries.

    mac_query or client_query anchor the search (Netdisco's node search only returns
    "ips" entries for a MAC, IP, or hostname query); device_query narrows by router
    name/IP afterwards, since routers aren't a queryable primitive of their own here.

    Returns (rows, total_count, truncated).
    """
    if mac_query:
        query = mac_query
    elif client_query:
        query = client_query
    else:
        return [], 0, False

    since = date_from.isoformat() if date_from else None
    until = date_to.isoformat() if date_to else None
    require_active = not (date_from or date_to)

    entries = []
    for b in backends:
        entries.extend(b.arp_entries(query, partial=False, since=since, until=until))

    if require_active:
        entries = [e for e in entries if e.get("active")]
    if device_query:
        q = device_query.lower()
        entries = [e for e in entries if q in (e.get("router_name") or "").lower()]

    entries = _apply_date_filter(entries, date_from, date_to)

    rows = [
        {
            "router_name": e.get("router_name"),
            "router_ip": e.get("router_ip"),
            "mac": e.get("mac"),
            "client_ip": e.get("ip"),
            "client_name": e.get("dns"),
            "vendor": _clean_vendor(e.get("vendor")),
            "active": e.get("active"),
            "time_first": e.get("time_first"),
            "time_last": e.get("time_last"),
        }
        for e in entries
    ]
    rows.sort(key=lambda r: r.get("time_last") or "", reverse=True)
    total = len(rows)
    truncated = total > max_rows
    return rows[:max_rows], total, truncated
