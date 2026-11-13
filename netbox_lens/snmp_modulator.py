import requests

# Longer than snmp-modulator's own default MODULATOR_WAIT_TIMEOUT (60s), so
# our client doesn't time out before the server has a chance to return its
# own 503 (queue busy) response.
DEFAULT_TIMEOUT = 90


def probe(config, device_ip, dry_run=True, wait=True):
    """
    Call snmp-modulator's /probe endpoint.

    wait=True blocks (up to the server's own wait-timeout) and returns the
    full ModulationResult inline (HTTP 200), or a meaningful non-200 status:
    400 (bad host), 401 (auth), 404 (device not found/ineligible), 409
    (a probe for this host is already in progress), or 503 (the server's
    worker pool is saturated and no slot freed up in time).

    wait=False queues the probe in the background and returns immediately
    (HTTP 202) with status "queued" or "skipped" (already in progress) —
    no result is available this way.

    dry_run=True previews without persisting; dry_run=False writes the
    resolved SNMP modules/auth profile back to NetBox's custom fields
    (only when something actually changed).

    Returns (ok, status_code, data, error). ok=True for any well-formed
    JSON response regardless of status code — 409/503/etc. are meaningful,
    actionable states for the caller to branch on, not transport failures.
    ok=False only for connection/timeout/unparseable-body errors.
    """
    base_url = config.get("url", "").rstrip("/")
    if not base_url:
        return False, None, None, "SNMP Modulator URL is not configured."
    headers = {"Accept": "application/json"}
    token = config.get("token", "")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        resp = requests.get(
            f"{base_url}/probe",
            headers=headers,
            params={
                "host": device_ip,
                "wait": "true" if wait else "false",
                "dry_run": "true" if dry_run else "false",
            },
            timeout=config.get("timeout", DEFAULT_TIMEOUT),
            verify=config.get("verify_ssl", True),
        )
        try:
            data = resp.json() if resp.content else {}
        except ValueError:
            return False, resp.status_code, None, f"SNMP Modulator returned a non-JSON response (HTTP {resp.status_code})."
        return True, resp.status_code, data, None
    except requests.exceptions.RequestException as exc:
        return False, None, None, str(exc)


def health(config):
    """
    Call snmp-modulator's /health liveness endpoint (unauthenticated).
    Returns {"status": "ok", "version": <str>, "probe_in_progress": <int>,
    "in_flight": [<job-key>, ...], "netbox_only_module_count": <int>,
    "snmp_exporter_only_module_count": <int>} — job keys are strings like
    "host:<ip>" or "netbox:<filter>=<value>&...". The two mismatch counts
    are a cheap preview of /stats's full module_mismatch lists.

    Returns (ok, data, error).
    """
    base_url = config.get("url", "").rstrip("/")
    if not base_url:
        return False, None, "SNMP Modulator URL is not configured."
    try:
        resp = requests.get(
            f"{base_url}/health",
            headers={"Accept": "application/json"},
            timeout=config.get("timeout", 15),
            verify=config.get("verify_ssl", True),
        )
        resp.raise_for_status()
        data = resp.json() if resp.content else {}
        return True, data, None
    except requests.exceptions.RequestException as exc:
        return False, None, str(exc)


def stats(config):
    """
    Call snmp-modulator's /stats endpoint (unauthenticated, same tier as
    /health) for the full breakdowns /health only summarizes as counts:
    devices_by_auth_profile ({profile: count}), devices_by_module
    ({module: count}), and module_mismatch ({netbox_only: [...],
    snmp_exporter_only: [...], netbox_only_count, snmp_exporter_only_count}).

    Returns (ok, data, error).
    """
    base_url = config.get("url", "").rstrip("/")
    if not base_url:
        return False, None, "SNMP Modulator URL is not configured."
    try:
        resp = requests.get(
            f"{base_url}/stats",
            headers={"Accept": "application/json"},
            timeout=config.get("timeout", 15),
            verify=config.get("verify_ssl", True),
        )
        resp.raise_for_status()
        data = resp.json() if resp.content else {}
        return True, data, None
    except requests.exceptions.RequestException as exc:
        return False, None, str(exc)


def probe_devices(config, device_ids):
    """
    Call snmp-modulator's /probe/netbox with the given NetBox device ids (sent
    as id filters): queues one background probe over exactly those devices
    and returns at once — {"status": "queued"|"skipped", "filter", "reason"?}.
    It runs in the server's own mode (MODULATOR_DRY_RUN; false in our
    deployments, so it writes the resolved module/auth profile to NetBox).
    Devices without the auth-profile CF are skipped by the modulator.

    Returns (ok, data, error).
    """
    base_url = config.get("url", "").rstrip("/")
    if not base_url:
        return False, None, "SNMP Modulator URL is not configured."
    headers = {"Accept": "application/json"}
    token = config.get("token", "")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        resp = requests.post(
            f"{base_url}/probe/netbox",
            headers=headers,
            params=[("id", str(i)) for i in device_ids],
            timeout=config.get("timeout", 30),
            verify=config.get("verify_ssl", True),
        )
        resp.raise_for_status()
        data = resp.json() if resp.content else {}
        return True, data, None
    except requests.exceptions.RequestException as exc:
        return False, None, str(exc)


def _probe_run(config, method, params=None):
    """One call to snmp-modulator's /probe/run (bulk probe with dry run).
    Returns (ok, data, error); a 404 on GET means this modulator has no
    /probe/run yet, flagged in data as {"unsupported": True}."""
    base_url = config.get("url", "").rstrip("/")
    if not base_url:
        return False, None, "SNMP Modulator URL is not configured."
    headers = {"Accept": "application/json"}
    token = config.get("token", "")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        resp = requests.request(
            method, f"{base_url}/probe/run", headers=headers, params=params or [],
            timeout=config.get("status_timeout", 15), verify=config.get("verify_ssl", True),
        )
        if resp.status_code == 404 and method == "GET":
            return False, {"unsupported": True}, "This SNMP Modulator has no bulk probe run yet."
        if resp.status_code >= 400:
            try:
                body = resp.json()
                reason = body.get("detail") or body.get("error") or body.get("reason")
            except ValueError:
                reason = None
            if resp.status_code == 409:
                reason = reason or "A bulk probe is already running."
            return False, None, reason or f"HTTP {resp.status_code} from SNMP Modulator"
        return True, resp.json() if resp.content else {}, None
    except requests.exceptions.RequestException as exc:
        return False, None, str(exc)


def probe_run_start(config, device_ids, dry_run=True):
    """POST /probe/run for these NetBox device ids: one run, one device after
    the other, capped at the modulator's max_devices (the rest is over_limit).
    Applying ids from a recent dry run commits its cached result."""
    params = [("id", str(i)) for i in device_ids]
    params.append(("dry_run", "true" if dry_run else "false"))
    return _probe_run(config, "POST", params)


def probe_run_status(config):
    """GET /probe/run: the current or last run — status running|done|cancelled|
    interrupted|none, dry_run, started, finished, max_devices, flat totals,
    avg_duration_s, devices[{device_id, device_name, host, status, changed,
    previous_modules, final_modules, auth_profile_old/new, reason, duration_s,
    source}]."""
    return _probe_run(config, "GET")


def probe_run_cancel(config):
    """DELETE /probe/run: drops the queued devices; the one running finishes."""
    return _probe_run(config, "DELETE")
