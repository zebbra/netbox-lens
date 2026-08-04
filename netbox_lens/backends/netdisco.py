import os
from datetime import date

import requests

from .base import BackendStatus, LensBackend, SearchResult


class NetdiscoBackend(LensBackend):
    name = "netdisco"
    label = "Netdisco"
    icon = "mdi mdi-network"

    def search(self, query: str, partial: bool = False, archived: bool = False, since: str | None = None) -> SearchResult:
        result = SearchResult(backend=self.name, label=self.label, icon=self.icon)

        base_url = self.config.get("url", "").rstrip("/")
        if not base_url:
            result.error = "Netdisco URL is not configured."
            return result

        params = {
            "q": query,
            "partial": "true" if partial else "false",
            "deviceports": "true",
            "show_vendor": "true",
            "archived": "true" if archived else "false",
        }
        if since:
            params["daterange"] = f"{since} - {date.today().isoformat()}"

        try:
            resp = requests.get(
                f"{base_url}/api/v1/search/node",
                headers={
                    "Authorization": f"Bearer {os.environ.get('LENS_NETDISCO_TOKEN', self.config.get('token', ''))}",
                    "Accept": "application/json",
                },
                params=params,
                timeout=self.config.get("timeout", 15),
                verify=self.config.get("verify_ssl", True),
            )
            resp.raise_for_status()
            data = resp.json() if resp.content else {}
            if not isinstance(data, dict):
                data = {}
            result.sightings = data.get("sightings") or []
            result.ips = data.get("ips") or []
            result.macs = data.get("macs") or []

            # Always fetch the full port sighting history per MAC without a
            # daterange — the initial search may have been date-filtered but
            # sightings are most useful as a complete timeline.
            seen_macs = (
                {m["mac"] for m in result.macs     if m.get("mac")}
                | {s["mac"] for s in result.sightings if s.get("mac")}
            )
            if seen_macs:
                result.sightings = []
                headers = {
                    "Authorization": f"Bearer {os.environ.get('LENS_NETDISCO_TOKEN', self.config.get('token', ''))}",
                    "Accept": "application/json",
                }
                for mac in seen_macs:
                    follow_params = {"q": mac, "archived": "true" if archived else "false", "deviceports": "false"}
                    if since:
                        follow_params["daterange"] = f"{since} - {date.today().isoformat()}"
                    r2 = requests.get(
                        f"{base_url}/api/v1/search/node",
                        headers=headers,
                        params=follow_params,
                        timeout=self.config.get("timeout", 15),
                        verify=self.config.get("verify_ssl", True),
                    )
                    if r2.ok:
                        d2 = r2.json() if r2.content else {}
                        result.sightings.extend(d2.get("sightings") or [])

            # Device name/hostname matching is a separate Netdisco entity from
            # node/MAC sightings — query it too so switch/router hostnames
            # (not just end-host MACs/IPs) are actually searchable.
            try:
                dresp = requests.get(
                    f"{base_url}/api/v1/search/device",
                    headers={
                        "Authorization": f"Bearer {os.environ.get('LENS_NETDISCO_TOKEN', self.config.get('token', ''))}",
                        "Accept": "application/json",
                    },
                    params={"q": query},
                    timeout=self.config.get("timeout", 15),
                    verify=self.config.get("verify_ssl", True),
                )
                if dresp.ok:
                    ddata = dresp.json() if dresp.content else []
                    result.devices = ddata if isinstance(ddata, list) else []
            except Exception:
                pass

        except requests.ConnectionError:
            result.error = "Could not reach Netdisco — check the configured URL."
        except requests.Timeout:
            result.error = "Netdisco did not respond in time."
        except requests.HTTPError as e:
            status = e.response.status_code
            if status == 401:
                result.error = "Netdisco rejected the API token (401 Unauthorized)."
            elif status == 404:
                result.error = "Netdisco API endpoint not found — check the configured URL."
            else:
                result.error = f"Netdisco returned HTTP {status}."
        except Exception as e:
            result.error = str(e)

        return result

    def device_nodes(self, device_ip: str) -> list:
        base_url = self.config.get("url", "").rstrip("/")
        if not base_url:
            return []
        try:
            resp = requests.get(
                f"{base_url}/api/v1/object/device/{device_ip}/nodes",
                headers={
                    "Authorization": f"Bearer {os.environ.get('LENS_NETDISCO_TOKEN', self.config.get('token', ''))}",
                    "Accept": "application/json",
                },
                params={"active_only": "true"},
                timeout=self.config.get("timeout", 15),
                verify=self.config.get("verify_ssl", True),
            )
            resp.raise_for_status()
            data = resp.json() if resp.content else []
            return data if isinstance(data, list) else []
        except Exception:
            return []

    def device_neighbors(self, device_ip: str) -> list:
        base_url = self.config.get("url", "").rstrip("/")
        if not base_url:
            return []
        try:
            resp = requests.get(
                f"{base_url}/api/v1/object/device/{device_ip}/ports",
                headers={
                    "Authorization": f"Bearer {os.environ.get('LENS_NETDISCO_TOKEN', self.config.get('token', ''))}",
                    "Accept": "application/json",
                },
                timeout=self.config.get("timeout", 15),
                verify=self.config.get("verify_ssl", True),
            )
            resp.raise_for_status()
            data = resp.json() if resp.content else []
            if not isinstance(data, list):
                return []
            return [
                {
                    "port": p.get("port"),
                    "remote_port": p.get("remote_port"),
                    "remote_ip": p.get("remote_ip"),
                    "remote_type": p.get("remote_type"),
                    "remote_id": p.get("remote_id"),
                }
                for p in data
                if p.get("remote_ip") or p.get("remote_id")
            ]
        except Exception:
            return []

    def device_summary(self, device_ip: str) -> dict:
        base_url = self.config.get("url", "").rstrip("/")
        if not base_url:
            return {}
        try:
            resp = requests.get(
                f"{base_url}/api/v1/object/device/{device_ip}",
                headers={
                    "Authorization": f"Bearer {os.environ.get('LENS_NETDISCO_TOKEN', self.config.get('token', ''))}",
                    "Accept": "application/json",
                },
                timeout=self.config.get("timeout", 15),
                verify=self.config.get("verify_ssl", True),
            )
            resp.raise_for_status()
            data = resp.json() if resp.content else {}
            if not isinstance(data, dict):
                return {}
            return {
                "model": data.get("model"),
                "os": data.get("os"),
                "os_ver": data.get("os_ver"),
                "first_discovered": data.get("creation"),
                "last_discover": data.get("last_discover"),
                "last_macsuck": data.get("last_macsuck"),
                "last_arpnip": data.get("last_arpnip"),
            }
        except Exception:
            return {}

    def device_web_url(self, device_ip: str) -> str | None:
        web_url = self.config.get("web_url", "").rstrip("/")
        if not web_url:
            return None
        return f"{web_url}/device?tab=details&q={device_ip}"

    def trigger_discover(self, device_ip: str) -> tuple[bool, str]:
        base_url = self.config.get("url", "").rstrip("/")
        if not base_url:
            return False, "Netdisco URL is not configured."
        admin_token = os.environ.get("LENS_NETDISCO_ADMIN_TOKEN", self.config.get("admin_token", ""))
        if not admin_token:
            return False, "No Netdisco admin token configured for triggering jobs."
        try:
            resp = requests.post(
                f"{base_url}/api/v1/queue/jobs",
                headers={
                    "Authorization": f"Bearer {admin_token}",
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                },
                json=[{"action": "discover", "device": device_ip}],
                timeout=self.config.get("timeout", 15),
                verify=self.config.get("verify_ssl", True),
                allow_redirects=False,
            )
            if resp.status_code in (301, 302, 303):
                return False, "Netdisco rejected the admin token (insufficient role for job triggering)."
            resp.raise_for_status()
            data = resp.json() if resp.content else {}
            if isinstance(data, dict) and data.get("success"):
                return True, f"Discover job queued for {device_ip}."
            return False, "Netdisco did not confirm the job was queued."
        except requests.ConnectionError:
            return False, "Could not reach Netdisco — check the configured URL."
        except requests.Timeout:
            return False, "Netdisco did not respond in time."
        except requests.HTTPError as e:
            status = e.response.status_code
            if status == 401:
                return False, "Netdisco rejected the admin token (401 Unauthorized)."
            elif status == 403:
                return False, "Netdisco admin token lacks permission to queue jobs (403 Forbidden)."
            return False, f"Netdisco returned HTTP {status}."
        except Exception as e:
            return False, str(e)

    def status(self) -> BackendStatus:
        s = BackendStatus(backend=self.name, label=self.label, icon=self.icon)
        base_url = self.config.get("url", "").rstrip("/")
        if not base_url:
            s.error = "Netdisco URL is not configured."
            return s
        try:
            resp = requests.get(
                f"{base_url}/api/v1/statistics",
                headers={"Authorization": f"Bearer {os.environ.get('LENS_NETDISCO_TOKEN', self.config.get('token', ''))}"},
                timeout=self.config.get("timeout", 15),
                verify=self.config.get("verify_ssl", True),
            )
            resp.raise_for_status()
            s.stats = resp.json()
        except requests.ConnectionError:
            s.error = "Could not reach Netdisco."
        except requests.Timeout:
            s.error = "Netdisco did not respond in time."
        except requests.HTTPError as e:
            s.error = f"HTTP {e.response.status_code}"
        except Exception as e:
            s.error = str(e)
        return s
