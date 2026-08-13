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
    Returns {"status": "ok", "probe_in_progress": <int>, "in_flight": [<job-key>, ...]}
    — job keys are strings like "host:<ip>" or "netbox:<filter>=<value>&...".

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
