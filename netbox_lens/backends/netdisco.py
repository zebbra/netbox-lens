import os
from datetime import date

import requests

from .base import BackendStatus, LensBackend, SearchResult


class NetdiscoBackend(LensBackend):
    name = "netdisco"
    label = "Netdisco"
    icon = "mdi mdi-network"

    def search(
        self, query: str, partial: bool = False, archived: bool = False,
        since: str | None = None, until: str | None = None,
    ) -> SearchResult:
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
            params["daterange"] = f"{since} - {until or date.today().isoformat()}"

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
                        follow_params["daterange"] = f"{since} - {until or date.today().isoformat()}"
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

    def device_nodes(self, device_ip: str, since: str | None = None, until: str | None = None) -> list:
        base_url = self.config.get("url", "").rstrip("/")
        if not base_url:
            return []
        params = {"active_only": "false" if since else "true"}
        if since:
            params["daterange"] = f"{since} - {until or date.today().isoformat()}"
        try:
            resp = requests.get(
                f"{base_url}/api/v1/object/device/{device_ip}/nodes",
                headers={
                    "Authorization": f"Bearer {os.environ.get('LENS_NETDISCO_TOKEN', self.config.get('token', ''))}",
                    "Accept": "application/json",
                },
                params=params,
                timeout=self.config.get("timeout", 15),
                verify=self.config.get("verify_ssl", True),
            )
            resp.raise_for_status()
            data = resp.json() if resp.content else []
            return data if isinstance(data, list) else []
        except Exception:
            return []

    def search_ports(self, query: str, partial: bool = True) -> list:
        base_url = self.config.get("url", "").rstrip("/")
        if not base_url:
            return []
        try:
            resp = requests.get(
                f"{base_url}/api/v1/search/port",
                headers={
                    "Authorization": f"Bearer {os.environ.get('LENS_NETDISCO_TOKEN', self.config.get('token', ''))}",
                    "Accept": "application/json",
                },
                params={"q": query, "partial": "true" if partial else "false", "descr": "true"},
                timeout=self.config.get("timeout", 15),
                verify=self.config.get("verify_ssl", True),
            )
            resp.raise_for_status()
            data = resp.json() if resp.content else []
            return data if isinstance(data, list) else []
        except Exception:
            return []

    def node_sightings(
        self, query: str, partial: bool = False,
        since: str | None = None, until: str | None = None,
    ) -> list:
        base_url = self.config.get("url", "").rstrip("/")
        if not base_url:
            return []
        params = {"q": query, "partial": "true" if partial else "false", "deviceports": "false", "archived": "true"}
        if since:
            params["daterange"] = f"{since} - {until or date.today().isoformat()}"
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
                return []
            return [
                {
                    "mac": s.get("mac"),
                    "port": s.get("port"),
                    "vlan": s.get("vlan"),
                    "active": s.get("active"),
                    "time_first": s.get("time_first"),
                    "time_last": s.get("time_last"),
                    "_device_ip": s.get("switch"),
                    "_device_name": (s.get("device") or {}).get("name"),
                }
                for s in (data.get("sightings") or [])
            ]
        except Exception:
            return []

    def find_macs(self, query: str, partial: bool = True) -> list:
        base_url = self.config.get("url", "").rstrip("/")
        if not base_url:
            return []
        try:
            resp = requests.get(
                f"{base_url}/api/v1/search/node",
                headers={
                    "Authorization": f"Bearer {os.environ.get('LENS_NETDISCO_TOKEN', self.config.get('token', ''))}",
                    "Accept": "application/json",
                },
                params={"q": query, "partial": "true" if partial else "false", "deviceports": "false", "archived": "true"},
                timeout=self.config.get("timeout", 15),
                verify=self.config.get("verify_ssl", True),
            )
            resp.raise_for_status()
            data = resp.json() if resp.content else {}
            if not isinstance(data, dict):
                return []
            macs = {m.get("mac") for m in (data.get("macs") or []) if m.get("mac")}
            macs |= {m.get("mac") for m in (data.get("ips") or []) if m.get("mac")}
            return list(macs)
        except Exception:
            return []

    def arp_entries(
        self, query: str, partial: bool = False,
        since: str | None = None, until: str | None = None,
    ) -> list:
        base_url = self.config.get("url", "").rstrip("/")
        if not base_url:
            return []
        params = {"q": query, "partial": "true" if partial else "false", "deviceports": "false", "archived": "true"}
        if since:
            params["daterange"] = f"{since} - {until or date.today().isoformat()}"
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
                return []
            # Netdisco puts ARP-level results under "ips" for a MAC query but under
            # "macs" for an IP/hostname query — read both, like find_macs() does.
            raw = (data.get("ips") or []) + (data.get("macs") or [])
            return [
                {
                    "mac": e.get("mac"),
                    "ip": e.get("ip"),
                    "dns": e.get("dns"),
                    "router_ip": e.get("router_ip"),
                    "router_name": e.get("router_name") or e.get("router_ip"),
                    "vendor": (e.get("manufacturer") or {}).get("company"),
                    "active": e.get("active"),
                    "time_first": e.get("time_first"),
                    "time_last": e.get("time_last"),
                }
                for e in raw
            ]
        except Exception:
            return []

    def resolve_mac(self, mac: str) -> dict | None:
        base_url = self.config.get("url", "").rstrip("/")
        if not base_url:
            return None
        try:
            resp = requests.get(
                f"{base_url}/api/v1/search/node",
                headers={
                    "Authorization": f"Bearer {os.environ.get('LENS_NETDISCO_TOKEN', self.config.get('token', ''))}",
                    "Accept": "application/json",
                },
                params={"q": mac, "partial": "false", "deviceports": "false", "show_vendor": "false", "archived": "true"},
                timeout=self.config.get("timeout", 15),
                verify=self.config.get("verify_ssl", True),
            )
            resp.raise_for_status()
            data = resp.json() if resp.content else {}
            if not isinstance(data, dict):
                return None
            entry = (data.get("macs") or data.get("ips") or [None])[0]
            if not entry:
                return None
            return {"ip": entry.get("ip"), "dns": entry.get("dns")}
        except Exception:
            return None

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

    def device_ports(self, device_ip: str) -> list:
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
                    # Netdisco's own "descr" column is the raw SNMP ifDescr (same
                    # as the port name); the human-set description lives in "name".
                    "descr": p.get("name"),
                    "up": p.get("up"),
                    "up_admin": p.get("up_admin"),
                    "vlan": p.get("vlan"),
                }
                for p in data
            ]
        except Exception:
            return []

    def port_pae(self, device_ip: str, port: str) -> dict:
        base_url = self.config.get("url", "").rstrip("/")
        if not base_url:
            return {}
        try:
            resp = requests.get(
                f"{base_url}/api/v1/object/device/{device_ip}/port/{port}/properties",
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
                "authconfig_state": data.get("pae_authconfig_state"),
                "port_control": data.get("pae_authconfig_port_control"),
                "port_status": data.get("pae_authconfig_port_status"),
                "user": data.get("pae_authsess_user"),
                "mab": data.get("pae_authsess_mab"),
                "last_eapol_source": data.get("pae_last_eapol_frame_source"),
                "is_authenticator": data.get("pae_is_authenticator"),
                "is_supplicant": data.get("pae_is_supplicant"),
            }
        except Exception:
            return {}

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
                "pae_enabled": data.get("pae_is_enabled"),
            }
        except Exception:
            return {}

    def device_web_url(self, device_ip: str) -> str | None:
        web_url = self.config.get("web_url", "").rstrip("/")
        if not web_url:
            return None
        return f"{web_url}/device?tab=details&q={device_ip}"

    def _trigger_job(self, action: str, device_ip: str) -> tuple[bool, str]:
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
                json=[{"action": action, "device": device_ip}],
                timeout=self.config.get("timeout", 15),
                verify=self.config.get("verify_ssl", True),
                allow_redirects=False,
            )
            if resp.status_code in (301, 302, 303):
                return False, "Netdisco rejected the admin token (insufficient role for job triggering)."
            resp.raise_for_status()
            data = resp.json() if resp.content else {}
            if isinstance(data, dict) and data.get("success"):
                return True, f"{action.capitalize()} job queued for {device_ip}."
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

    def trigger_discover(self, device_ip: str) -> tuple[bool, str]:
        return self._trigger_job("discover", device_ip)

    def trigger_macsuck(self, device_ip: str) -> tuple[bool, str]:
        return self._trigger_job("macsuck", device_ip)

    def trigger_arpnip(self, device_ip: str) -> tuple[bool, str]:
        return self._trigger_job("arpnip", device_ip)

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
