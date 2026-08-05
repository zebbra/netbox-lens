from concurrent.futures import ThreadPoolExecutor, as_completed

try:
    from dcim.models import Device as NbDevice
except ImportError:
    NbDevice = None

MAX_DEVICE_MATCHES = 10
MAX_PORT_SCAN = 100
MAX_ROWS = 200


def _device_targets(device_query):
    if not NbDevice:
        return [], False
    qs = NbDevice.objects.filter(name__icontains=device_query).select_related("primary_ip4")
    devices = list(qs[:MAX_DEVICE_MATCHES + 1])
    truncated = len(devices) > MAX_DEVICE_MATCHES
    devices = devices[:MAX_DEVICE_MATCHES]
    targets = [(str(d.primary_ip4.address.ip), d.name) for d in devices if d.primary_ip4]
    return targets, truncated


def build_nac_status(backends, device_query, interface_query=None, max_rows=MAX_ROWS):
    """
    Per-port 802.1X/PAE status (auth state, port-control mode, NAC user, MAB),
    plus the device-wide PAE-enabled flag.

    device_query is mandatory: unlike the NetBox-backed interface views, this
    needs one Netdisco call per port (no bulk endpoint exists for PAE data),
    so browsing without a device anchor would fan out across the whole fleet.

    Returns (rows, total_count, truncated, port_scan_truncated).
    """
    if not device_query or not backends:
        return [], 0, False, False

    device_targets, device_truncated = _device_targets(device_query)
    if not device_targets:
        return [], 0, False, False

    candidates = []  # (device_ip, device_name, port, descr)
    for ip, name in device_targets:
        ports = []
        for b in backends:
            ports.extend(b.device_ports(ip))
            break  # first backend with a working call wins, same as elsewhere
        for p in ports:
            port = p.get("port")
            if not port:
                continue
            if interface_query:
                q = interface_query.lower()
                if q not in port.lower() and q not in (p.get("descr") or "").lower():
                    continue
            candidates.append((ip, name, port, p.get("descr")))

    port_scan_truncated = len(candidates) > MAX_PORT_SCAN
    candidates = candidates[:MAX_PORT_SCAN]

    pae_enabled_by_device = {}
    with ThreadPoolExecutor() as executor:
        futures = {
            executor.submit(backends[0].device_summary, ip): (ip, name)
            for ip, name in device_targets
        }
        for future in as_completed(futures):
            ip, _ = futures[future]
            pae_enabled_by_device[ip] = (future.result() or {}).get("pae_enabled")

    rows = [None] * len(candidates)
    with ThreadPoolExecutor() as executor:
        futures = {
            executor.submit(backends[0].port_pae, ip, port): i
            for i, (ip, name, port, descr) in enumerate(candidates)
        }
        for future in as_completed(futures):
            i = futures[future]
            ip, name, port, descr = candidates[i]
            pae = future.result() or {}
            rows[i] = {
                "device_name": name,
                "device_ip": ip,
                "pae_enabled": pae_enabled_by_device.get(ip),
                "port": port,
                "descr": descr,
                "authconfig_state": pae.get("authconfig_state"),
                "port_control": pae.get("port_control"),
                "port_status": pae.get("port_status"),
                "user": pae.get("user"),
                "mab": pae.get("mab"),
                "is_authenticator": pae.get("is_authenticator"),
                "is_supplicant": pae.get("is_supplicant"),
                "last_eapol_source": pae.get("last_eapol_source"),
            }

    rows = [r for r in rows if r]
    rows.sort(key=lambda r: (r["device_name"], r["port"]))
    total = len(rows)
    truncated = total > max_rows
    return rows[:max_rows], total, truncated, (device_truncated or port_scan_truncated)
