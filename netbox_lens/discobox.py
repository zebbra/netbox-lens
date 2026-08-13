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
