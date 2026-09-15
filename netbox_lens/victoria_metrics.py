import requests

# RFC1213 ifOperStatus / ifAdminStatus enum values, as scraped by the
# snmp-exporter "if_updown_state" module (see interfaceUpDownState metric).
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
    Query the interfaceUpDownState metric for the given NetBox device ids in
    a single request and return (status, latest_sample_ts):

      status: {(device_id, if_name): {"admin": ..., "oper": ...}}, values as
      "up"/"down"/... strings, keyed by device_id as a string.

      latest_sample_ts: the newest sample timestamp (Unix epoch seconds,
      float) among the returned series, or None if there were no results —
      i.e. how fresh this data actually is, for display to the user.

    The scrape/relabel pipeline already drops admin-down series at ingest
    time, so a (device_id, if_name) pair missing from the result means
    either the interface is admin-down, or the device isn't covered by the
    if_updown_state SNMP module — callers can't tell those apart and should
    treat a miss as "unknown", not "down".
    """
    if not device_ids:
        return {}, None
    ids = "|".join(str(i) for i in device_ids)
    query = f'interfaceUpDownState{{netbox_id=~"{ids}"}}'
    results = query_instant(url, query, timeout=timeout, verify_tls=verify_tls)

    status = {}
    latest_ts = None
    for series in results:
        metric = series.get("metric", {})
        device_id = metric.get("netbox_id")
        if_name = metric.get("ifName")
        if not device_id or not if_name:
            continue
        try:
            sample_ts, sample_val = series["value"]
            oper_code = int(float(sample_val))
        except (KeyError, IndexError, TypeError, ValueError):
            continue
        try:
            ts = float(sample_ts)
            if latest_ts is None or ts > latest_ts:
                latest_ts = ts
        except (TypeError, ValueError):
            pass
        admin_code = None
        try:
            admin_code = int(metric.get("ifAdminStatus"))
        except (TypeError, ValueError):
            pass
        status[(str(device_id), if_name)] = {
            "oper": OPER_STATUS_LABELS.get(oper_code, str(oper_code)),
            "admin": ADMIN_STATUS_LABELS.get(admin_code, str(admin_code)) if admin_code else None,
        }
    return status, latest_ts
