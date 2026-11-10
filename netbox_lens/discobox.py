import requests

DEFAULT_TIMEOUT = 180


def rebuild_inventory(config, device_ip, dry_run=True):
    """
    Call discobox's /rebuild endpoint, which synchronously reconciles one
    device's NetBox inventory (interfaces it owns, modules, fans/PSUs, SFPs)
    to exactly match Netdisco's current view — deleting anything stale when
    dry_run is False.

    Returns (ok, data, error).
    """
    base_url = config.get("url", "").rstrip("/")
    if not base_url:
        return False, None, "Discobox URL is not configured."
    headers = {"Accept": "application/json"}
    token = config.get("token", "")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        resp = requests.get(
            f"{base_url}/rebuild",
            headers=headers,
            params={"host": device_ip, "dry_run": "true" if dry_run else "false"},
            timeout=config.get("timeout", DEFAULT_TIMEOUT),
            verify=config.get("verify_ssl", True),
        )
        resp.raise_for_status()
        data = resp.json() if resp.content else {}
        return True, data, None
    except requests.exceptions.RequestException as exc:
        return False, None, str(exc)


def health(config):
    """
    Call discobox's /health liveness endpoint (unauthenticated).
    Returns {"status": "ok", "paused": bool, "in_flight": [<host>, ...]}.
    "paused" is true if either manually paused or its circuit breaker has
    tripped from repeated upstream timeouts — there's no separate field
    distinguishing the two.

    Returns (ok, data, error).
    """
    base_url = config.get("url", "").rstrip("/")
    if not base_url:
        return False, None, "Discobox URL is not configured."
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
    Call discobox's /stats endpoint (unauthenticated, same tier as
    /health and /metrics) for a richer snapshot than /health carries:
    version, paused, in_flight (a count here, not the host list /health
    returns), last_reconcile (epoch seconds), unknown/not_in_netdisco/
    not_in_netbox/tag_mismatches counts, and a liveness sub-object
    ({"ok", "devices", "checked"}) when liveness_enabled.

    Returns (ok, data, error).
    """
    base_url = config.get("url", "").rstrip("/")
    if not base_url:
        return False, None, "Discobox URL is not configured."
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


def set_paused(config, paused):
    """
    Call discobox's /sync/pause or /sync/resume — pauses or resumes its
    background sync loop entirely (not scoped to a single device).
    Returns {"status": "paused"|"running", "queued": <int>}.

    Returns (ok, data, error).
    """
    base_url = config.get("url", "").rstrip("/")
    if not base_url:
        return False, None, "Discobox URL is not configured."
    headers = {"Accept": "application/json"}
    token = config.get("token", "")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    path = "/sync/pause" if paused else "/sync/resume"
    try:
        resp = requests.post(
            f"{base_url}{path}",
            headers=headers,
            timeout=config.get("timeout", 15),
            verify=config.get("verify_ssl", True),
        )
        resp.raise_for_status()
        data = resp.json() if resp.content else {}
        return True, data, None
    except requests.exceptions.RequestException as exc:
        return False, None, str(exc)


def sync_device(config, device_ip, force=False):
    """
    Call discobox's /sync endpoint, which queues a background job to sync
    Netdisco's data for one device into NetBox (database to database — this
    never contacts the device itself). Returns immediately with status
    "queued" or "skipped" (e.g. cooldown, already in progress).

    force bypasses discobox's sync cooldown window.

    Returns (ok, data, error).
    """
    base_url = config.get("url", "").rstrip("/")
    if not base_url:
        return False, None, "Discobox URL is not configured."
    headers = {"Accept": "application/json"}
    token = config.get("token", "")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        resp = requests.get(
            f"{base_url}/sync",
            headers=headers,
            params={"host": device_ip, "force": "true" if force else "false"},
            timeout=config.get("timeout", DEFAULT_TIMEOUT),
            verify=config.get("verify_ssl", True),
        )
        resp.raise_for_status()
        data = resp.json() if resp.content else {}
        return True, data, None
    except requests.exceptions.RequestException as exc:
        return False, None, str(exc)


def refresh_ip(config, device_id, dry_run=True):
    """
    Call discobox's /refresh-ip for one NetBox device: resolve its name (DNS,
    else Netdisco) and set primary_ip4. Dry run is a GET; applying must be a
    POST. Returns {"dry_run", "summary", "devices": [{"action": set|replace|
    unchanged|error, "old", "new", "source", "resolved", "interface",
    "address", "error", ...}]}.

    Returns (ok, data, error). discobox's 4xx/5xx carry a JSON reason, which
    is passed on as the error rather than the bare HTTP status.
    """
    base_url = config.get("url", "").rstrip("/")
    if not base_url:
        return False, None, "Discobox URL is not configured."
    headers = {"Accept": "application/json"}
    token = config.get("token", "")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    params = {"device": str(device_id), "dry_run": "true" if dry_run else "false"}
    try:
        resp = requests.request(
            "GET" if dry_run else "POST",
            f"{base_url}/refresh-ip",
            headers=headers,
            params=params,
            timeout=config.get("timeout", DEFAULT_TIMEOUT),
            verify=config.get("verify_ssl", True),
        )
        if resp.status_code >= 400:
            try:
                body = resp.json()
                reason = body.get("error") or body.get("detail") or body.get("reason")
            except ValueError:
                reason = None
            return False, None, reason or f"HTTP {resp.status_code} from discobox"
        return True, resp.json() if resp.content else {}, None
    except requests.exceptions.RequestException as exc:
        return False, None, str(exc)
