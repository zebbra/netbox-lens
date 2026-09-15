import requests

# RFC1213 ifOperStatus / ifAdminStatus enum values. Kept around for the
# interfaceUpDownState path below (currently disabled) — the ifOperStatus_info
# path doesn't need these since the enum value is already a string label.
OPER_STATUS_LABELS = {
    1: "up",
    2: "down",
    3: "testing",
    4: "unknown",
    5: "dormant",
    6: "notPresent",
    7: "lowerLayerDown",
}
ADMIN_STATUS_LABELS = {1: "up", 2: "down", 3: "testing"}


def query_instant(url, query, timeout=10, verify_tls=True):
    """
    Run an instant PromQL query against a Prometheus-compatible API
    (VictoriaMetrics vmselect, URL including its tenant prefix such as
    /select/1/prometheus). Raises on HTTP or payload errors — the caller
    decides fail-open behavior.
    """
    resp = requests.get(
        f"{url.rstrip('/')}/api/v1/query",
        params={"query": query},
        timeout=timeout,
        verify=verify_tls,
        headers={"Accept": "application/json", "User-Agent": "netbox-lens"},
    )
    resp.raise_for_status()
    payload = resp.json()
    if payload.get("status") != "success":
        raise RuntimeError(f"VictoriaMetrics query failed: {payload.get('error') or 'unknown error'}")
    return payload.get("data", {}).get("result", [])


def fetch_interface_updown_state(url, device_ids, timeout=10, verify_tls=True):
    """
    Query the given NetBox device ids' operational interface status in a
    single request and return (status, latest_sample_ts):

      status: {(device_id, if_name): {"admin": ..., "oper": ...}}, values as
      "up"/"down"/... strings, keyed by device_id as a string.

      latest_sample_ts: the newest sample timestamp (Unix epoch seconds,
      float) among the returned series, or None if there were no results —
      i.e. how fresh this data actually is, for display to the user.

    Uses ifOperStatus_info (an snmp-exporter EnumAsInfo metric — the state is
    the ifOperStatus label itself, sample value always 1), from the if_full /
    if_status modules, which have broad fleet coverage today. admin is left
    None here — NetBox's own enabled field is the admin-status source of
    truth elsewhere in this codebase, so it isn't needed from VM.
    """
    if not device_ids:
        return {}, None
    ids = "|".join(str(i) for i in device_ids)
    query = f'ifOperStatus_info{{netbox_id=~"{ids}"}}'
    results = query_instant(url, query, timeout=timeout, verify_tls=verify_tls)

    status = {}
    latest_ts = None
    for series in results:
        metric = series.get("metric", {})
        device_id = metric.get("netbox_id")
        if_name = metric.get("ifName")
        oper = metric.get("ifOperStatus")
        if not device_id or not if_name or not oper:
            continue
        try:
            ts = float(series["value"][0])
            if latest_ts is None or ts > latest_ts:
                latest_ts = ts
        except (KeyError, IndexError, TypeError, ValueError):
            pass
        status[(str(device_id), if_name)] = {"oper": oper, "admin": None}
    return status, latest_ts


# Once the if_updown_state SNMP-exporter module (interfaceUpDownState metric)
# has broad fleet coverage, it's the better source: it also carries
# ifAdminStatus as a label, and the ingest-time relabel pipeline already
# drops admin-down series, so a hit there means "genuinely admin-up but
# reporting this oper state" with no extra NetBox cross-check needed.
#
# def fetch_interface_updown_state(url, device_ids, timeout=10, verify_tls=True):
#     if not device_ids:
#         return {}, None
#     ids = "|".join(str(i) for i in device_ids)
#     query = f'interfaceUpDownState{{netbox_id=~"{ids}"}}'
#     results = query_instant(url, query, timeout=timeout, verify_tls=verify_tls)
#
#     status = {}
#     latest_ts = None
#     for series in results:
#         metric = series.get("metric", {})
#         device_id = metric.get("netbox_id")
#         if_name = metric.get("ifName")
#         if not device_id or not if_name:
#             continue
#         try:
#             sample_ts, sample_val = series["value"]
#             oper_code = int(float(sample_val))
#         except (KeyError, IndexError, TypeError, ValueError):
#             continue
#         try:
#             ts = float(sample_ts)
#             if latest_ts is None or ts > latest_ts:
#                 latest_ts = ts
#         except (TypeError, ValueError):
#             pass
#         admin_code = None
#         try:
#             admin_code = int(metric.get("ifAdminStatus"))
#         except (TypeError, ValueError):
#             pass
#         status[(str(device_id), if_name)] = {
#             "oper": OPER_STATUS_LABELS.get(oper_code, str(oper_code)),
#             "admin": ADMIN_STATUS_LABELS.get(admin_code, str(admin_code)) if admin_code else None,
#         }
#     return status, latest_ts
