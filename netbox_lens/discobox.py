import requests

DEFAULT_TIMEOUT = 60


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
