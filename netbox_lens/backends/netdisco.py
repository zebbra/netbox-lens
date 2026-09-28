import json
import os
import re
from datetime import date

import requests

from .base import BackendStatus, LensBackend, SearchResult

# snmp_polling_timeout is the SNMP modulator's whole-device Prometheus scrape
# budget ("20s"/"2m"/"5m"), not a per-request timeout — passed straight
# through, "2m" would make Netdisco wait 120s on every single SNMP request.
# So a discover gets a twelfth of it, clamped to 10–20s: 10s is what a big
# 9800 WLC needs (it fails at 3s), 20s caps the slowest boxes. Empty or
# unparseable falls back to the floor. Same constants as discobox, which
# derives its reconcile discovers the same way — keep the two in step.
SNMP_TIMEOUT_DIVISOR = 12
SNMP_TIMEOUT_FLOOR_US = 10_000_000
SNMP_TIMEOUT_CAP_US = 20_000_000
_TIMEOUT_UNITS_US = {"us": 1, "ms": 1_000, "s": 1_000_000, "m": 60_000_000, "h": 3_600_000_000}


def parse_snmp_timeout_us(value: str | None) -> int | None:
    """NetBox snmp_polling_timeout ("30s", "3m", "500ms", bare number = s) to
    microseconds. Copy of discobox's _parse_snmp_timeout_us (separate
    services, no shared package); None if empty or unparseable."""
    if not value:
        return None
    m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*(us|ms|s|m|h)?", str(value).strip(), re.IGNORECASE)
    if not m:
        return None
    return int(float(m.group(1)) * _TIMEOUT_UNITS_US[(m.group(2) or "s").lower()])


def discover_snmp_timeout_us(value: str | None) -> int:
    """Netdisco snmptimeout (µs, per SNMP request) for a discover, derived
    from the scrape budget in snmp_polling_timeout — see the constants above."""
    budget_us = parse_snmp_timeout_us(value)
    if budget_us is None:
        return SNMP_TIMEOUT_FLOOR_US
    return max(SNMP_TIMEOUT_FLOOR_US, min(SNMP_TIMEOUT_CAP_US, budget_us // SNMP_TIMEOUT_DIVISOR))

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
            # Netdisco returns node/IP rows under "ips" when the query resolved
            # via a MAC address match, or under "macs" when it resolved via
            # hostname/IP/vendor — same row shape either way, just two names
            # for the same dataset depending on which internal path matched.
            result.ips = (data.get("ips") or []) + (data.get("macs") or [])

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
                        sightings = d2.get("sightings") or []
                        # /search/node returns "wireless" (node_wireless rows)
                        # alongside "sightings" for the same query, at no extra
                        # request cost. No FK between the tables — correlate by
                        # MAC (there's only one MAC per follow-up call here).
                        # Skip ssid=="unknown": Netdisco writes that literal
                        # string when its macsuck worker's SNMP walk misses the
                        # SSID table for a client mid-association/roam, and
                        # since (mac, ssid) is node_wireless's primary key, it
                        # becomes a spurious permanent second row otherwise.
                        wireless = next(
                            (w for w in (d2.get("wireless") or []) if w.get("ssid") and w.get("ssid") != "unknown"),
                            None,
                        )
                        if wireless:
                            for s in sightings:
                                s["ssid"] = wireless.get("ssid")
                                s["sigstrength"] = wireless.get("sigstrength")
                        result.sightings.extend(sightings)

            # Device name/hostname matching is a separate Netdisco entity from
            # node/MAC sightings — query it too so switch/router hostnames
            # (not just end-host MACs/IPs) are actually searchable. Use the
            # "name" filter (Device.name only) rather than "q" — "q" is a
            # fuzzy match across contact/serial/location/description/dns and
            # any of the device's *other* interface IP aliases, so a router
            # with an unrelated client-facing SVI in the searched domain would
            # otherwise show up as a false-positive "device" match.
            try:
                dresp = requests.get(
                    f"{base_url}/api/v1/search/device",
                    headers={
                        "Authorization": f"Bearer {os.environ.get('LENS_NETDISCO_TOKEN', self.config.get('token', ''))}",
                        "Accept": "application/json",
                    },
                    params={"name": query},
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
            # /search/node already returns a "wireless" array (node_wireless
            # rows) alongside "sightings" for the same query, at no extra
            # request cost. There's no FK between the two tables — correlate
            # by MAC. Netdisco writes the literal ssid "unknown" when its
            # macsuck worker's SNMP walk misses the SSID table for that
            # client (association/roam race, or the controller not
            # populating it); since (mac, ssid) is node_wireless's primary
            # key, that becomes a second permanent row for the same client,
            # so it's filtered out here rather than treated as a real SSID.
            wireless_by_mac = {}
            for w in (data.get("wireless") or []):
                if not w.get("mac") or w.get("ssid") == "unknown":
                    continue
                wireless_by_mac.setdefault(w["mac"], w)
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
                    "ssid": (wireless_by_mac.get(s.get("mac")) or {}).get("ssid"),
                    "sigstrength": (wireless_by_mac.get(s.get("mac")) or {}).get("sigstrength"),
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
                    "type": p.get("type"),
                }
                for p in data
            ]
        except Exception:
            return []

    def wireless_ports(self, device_ip: str):
        """Radio port names for this device (device_port_wireless rows) — the
        cheapest wired-vs-wireless discriminator: a node's port is wireless
        iff it's in this set (WLC nodes report port as "<AP-MAC>.<radio>").
        One call per device, no per-port fan-out.

        Returns None if the call couldn't be answered (unreachable/misconfigured)
        — caller should treat that as "unknown", not "wired". Returns a set on
        success, which is legitimately empty for a plain switch with no radios
        at all — that's a confirmed "everything here is wired", not "unknown".
        """
        base_url = self.config.get("url", "").rstrip("/")
        if not base_url:
            return None
        try:
            resp = requests.get(
                f"{base_url}/api/v1/object/device/{device_ip}/wireless_ports",
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
                return None
            return {p.get("port") for p in data if p.get("port")}
        except Exception:
            return None

    def device_last_discover(self, device_ip: str):
        """Netdisco's device_port.up/up_admin are populated by its "discover"
        poller job, not a live SNMP call — this returns that job's last-run
        timestamp so callers can show how old the port state actually is."""
        base_url = self.config.get("url", "").rstrip("/")
        if not base_url:
            return None
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
            return data.get("last_discover") if isinstance(data, dict) else None
        except Exception:
            return None

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

    def _trigger_job(self, action: str, device_ip: str, extra: dict | None = None) -> tuple[bool, str]:
        base_url = self.config.get("url", "").rstrip("/")
        if not base_url:
            return False, "Netdisco URL is not configured."
        admin_token = os.environ.get("LENS_NETDISCO_ADMIN_TOKEN", self.config.get("admin_token", ""))
        if not admin_token:
            return False, "No Netdisco admin token configured for triggering jobs."
        job = {"action": action, "device": device_ip}
        if extra:
            job["extra"] = json.dumps(extra)
        try:
            resp = requests.post(
                f"{base_url}/api/v1/queue/jobs",
                headers={
                    "Authorization": f"Bearer {admin_token}",
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                },
                json=[job],
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

    def device_jobs(self, device_ip: str, limit: int = 50) -> list | None:
        """This device's jobs in Netdisco's admin queue, newest first
        (GET /api/v1/queue/jobs — api_admin role, so the admin token).
        Netdisco keeps status "queued" while a backend runs the job; it's
        only distinguishable from waiting by started_stamp being set.

        Returns None if the call couldn't be answered, [] if there are none.
        """
        base_url = self.config.get("url", "").rstrip("/")
        admin_token = os.environ.get("LENS_NETDISCO_ADMIN_TOKEN", self.config.get("admin_token", ""))
        if not base_url or not admin_token:
            return None
        try:
            resp = requests.get(
                f"{base_url}/api/v1/queue/jobs",
                headers={"Authorization": f"Bearer {admin_token}", "Accept": "application/json"},
                params={"device": device_ip, "limit": limit},
                timeout=self.config.get("timeout", 15),
                verify=self.config.get("verify_ssl", True),
                allow_redirects=False,
            )
            resp.raise_for_status()
            data = resp.json() if resp.content else []
            if not isinstance(data, list):
                return None
        except Exception:
            return None
        return [
            {
                "job": j.get("job"),
                "action": j.get("action"),
                "status": "running" if j.get("status") == "queued" and j.get("started_stamp") else j.get("status"),
                "port": j.get("port"),
                "subaction": j.get("subaction"),
                "entered": j.get("entered_stamp"),
                "started": j.get("started_stamp"),
                "finished": j.get("finished_stamp"),
                "duration": j.get("duration"),
                "backend": j.get("backend"),
                "username": j.get("username"),
                "log": j.get("log"),
            }
            for j in data
        ]

    def trigger_discover(self, device_ip: str, auth_profile: str | None = None,
                         snmp_timeout: str | None = None) -> tuple[bool, str]:
        # Keys in a job's extra override Netdisco settings for that job only
        # (Util/Configuration.pm parse_params_to_config). Same shape as
        # discobox's reconcile discovers (enqueue_discover), so a manual
        # discover behaves like the automatic one.
        # device_auth_tag_hint narrows Netdisco's SNMP credential attempts to
        # the matching tag in its own device_auth config, instead of trying
        # every configured community/credential in turn. An unknown/stale
        # hint is harmless — Netdisco falls back to trying all of them.
        timeout_us = discover_snmp_timeout_us(snmp_timeout)
        extra = {"snmptimeout": timeout_us, "skip_neighbor_queue": True}
        if auth_profile:
            extra["device_auth_tag_hint"] = auth_profile
        ok, message = self._trigger_job("discover", device_ip, extra=extra)
        if ok:
            if parse_snmp_timeout_us(snmp_timeout) is not None:
                source = f"from {snmp_timeout}"
            elif snmp_timeout:
                source = f"default, {snmp_timeout!r} not parseable"
            else:
                source = "default"
            message = message.rstrip(".") + f" (tag {auth_profile or '—'}, timeout {timeout_us / 1_000_000:g}s {source})."
        return ok, message

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
        url = f"{base_url}/api/v1/statistics"
        token = os.environ.get("LENS_NETDISCO_TOKEN", self.config.get("token", ""))
        if not token:
            s.error = f"LENS_NETDISCO_TOKEN is not set — no token to authenticate against {url}."
            return s
        try:
            resp = requests.get(
                url,
                headers={"Authorization": f"Bearer {token}"},
                timeout=self.config.get("timeout", 15),
                verify=self.config.get("verify_ssl", True),
            )
            resp.raise_for_status()
            s.stats = resp.json()
        except requests.ConnectionError:
            s.error = f"Could not reach Netdisco at {url}."
        except requests.Timeout:
            s.error = f"Netdisco did not respond in time ({url})."
        except requests.HTTPError as e:
            status = e.response.status_code
            if status in (401, 403):
                s.error = f"HTTP {status} from {url} — LENS_NETDISCO_TOKEN was rejected (missing, expired, or IP-restricted)."
            else:
                s.error = f"HTTP {status} from {url}."
        except Exception as e:
            s.error = str(e)
        return s
