import json
import os
import re
from concurrent.futures import ThreadPoolExecutor
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


# The search fetches each matched MAC's full sighting history with one extra
# call per MAC. A broad wildcard can match hundreds, so past this many only
# the sightings from the search itself are shown.
MAX_SIGHTING_FOLLOWUPS = 20


def _search_timeout_hint(query, since, until, timeout):
    """What to change when a search timed out, from what made it expensive."""
    hints = []
    if query.startswith("*") and query.strip("*"):
        core = query.strip("*")
        hints.append(f'anchor the wildcard, e.g. "{core}*" rather than "{query}"')
    elif "*" in query:
        hints.append("use fewer wildcards")
    if since:
        try:
            days = (date.fromisoformat(until or date.today().isoformat()) - date.fromisoformat(since)).days + 1
            hints.append(f"narrow the date range (now {days} days)")
        except ValueError:
            hints.append("narrow the date range")
    if not hints:
        return f"Netdisco did not answer within {timeout}s — it may be busy, try again in a moment."
    return f"Netdisco did not answer within {timeout}s — " + " or ".join(hints) + "."


def _apply_ssid(sightings, wireless_rows):
    """Copy SSID/signal from /search/node's "wireless" rows onto sightings of
    the same MAC (no FK between the tables). ssid "unknown" is skipped:
    Netdisco writes that literal when its macsuck SNMP walk misses the SSID
    table mid-roam, and as (mac, ssid) is node_wireless's key it lingers as
    a spurious second row."""
    by_mac = {}
    for w in wireless_rows or []:
        if w.get("mac") and w.get("ssid") and w.get("ssid") != "unknown":
            by_mac.setdefault(w["mac"].lower(), w)
    for s in sightings:
        w = by_mac.get((s.get("mac") or "").lower())
        if w:
            s["ssid"] = w.get("ssid")
            s["sigstrength"] = w.get("sigstrength")


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


    def _viewer_token(self) -> str:
        # Read-only token for lookups. LENS_NETDISCO_TOKEN is the pre-1.0.29
        # name, still read so existing deployments keep working.
        return (
            os.environ.get("LENS_NETDISCO_VIEWER_TOKEN")
            or os.environ.get("LENS_NETDISCO_TOKEN")
            or self.config.get("token", "")
        )

    def _admin_token(self) -> str:
        # Job queue (discover/macsuck/arpnip, Netdisco Jobs tab): the token's
        # Netdisco user needs users.admin = true (role api_admin).
        return os.environ.get("LENS_NETDISCO_ADMIN_TOKEN") or self.config.get("admin_token", "")

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
                    "Authorization": f"Bearer {self._viewer_token()}",
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

            # Port sightings come back with the search itself only when it
            # matched on a MAC (then with that MAC's node_wireless rows too).
            # A hostname/IP match returns just the IP rows, so the sightings
            # of those MACs need one follow-up call each: capped, because a
            # broad wildcard can match hundreds. The archived history is only
            # loaded on request (sighting_history, "Load history" button).
            seen_macs = sorted(
                {m["mac"] for m in result.ips if m.get("mac")}
                | {s["mac"] for s in result.sightings if s.get("mac")}
            )
            result.history_macs = seen_macs[:MAX_SIGHTING_FOLLOWUPS]
            if len(seen_macs) > MAX_SIGHTING_FOLLOWUPS:
                result.notice = (
                    f"{len(seen_macs)} MACs matched — port sightings and history are only loaded for up to "
                    f"{MAX_SIGHTING_FOLLOWUPS}. Narrow the search to see them all."
                )
            if result.sightings:
                _apply_ssid(result.sightings, data.get("wireless"))
            elif seen_macs:
                result.sightings = self._sightings_for_macs(result.history_macs, archived, since, until)

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
                        "Authorization": f"Bearer {self._viewer_token()}",
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
            result.error = _search_timeout_hint(query, since, until, self.config.get("timeout", 15))
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
                    "Authorization": f"Bearer {self._viewer_token()}",
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

    def _mac_sightings(self, mac, archived, since=None, until=None):
        """All sightings of one exact MAC, with its SSID where known."""
        params = {"q": mac, "archived": "true" if archived else "false", "deviceports": "false"}
        if since:
            params["daterange"] = f"{since} - {until or date.today().isoformat()}"
        resp = requests.get(
            f"{self.config.get('url', '').rstrip('/')}/api/v1/search/node",
            headers={"Authorization": f"Bearer {self._viewer_token()}", "Accept": "application/json"},
            params=params,
            timeout=self.config.get("timeout", 15),
            verify=self.config.get("verify_ssl", True),
        )
        if not resp.ok:
            return []
        data = resp.json() if resp.content else {}
        sightings = data.get("sightings") or []
        _apply_ssid(sightings, data.get("wireless"))
        return sightings

    def _sightings_for_macs(self, macs, archived, since=None, until=None):
        sightings = []
        with ThreadPoolExecutor(max_workers=8) as executor:
            for rows in executor.map(lambda m: self._mac_sightings(m, archived, since, until), macs):
                sightings.extend(rows)
        sightings.sort(key=lambda s: s.get("time_last") or "", reverse=True)
        return sightings

    def sighting_history(self, macs):
        """Archived-included sightings for up to MAX_SIGHTING_FOLLOWUPS MACs.
        Returns (sightings, error)."""
        if not self.config.get("url"):
            return [], "Netdisco URL is not configured."
        try:
            return self._sightings_for_macs(list(macs)[:MAX_SIGHTING_FOLLOWUPS], archived=True), None
        except requests.Timeout:
            return [], f"Netdisco did not answer within {self.config.get('timeout', 15)}s loading the history."
        except requests.RequestException as e:
            return [], str(e)

    def search_ports(self, query: str, partial: bool = True) -> list:
        base_url = self.config.get("url", "").rstrip("/")
        if not base_url:
            return []
        try:
            resp = requests.get(
                f"{base_url}/api/v1/search/port",
                headers={
                    "Authorization": f"Bearer {self._viewer_token()}",
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
                    "Authorization": f"Bearer {self._viewer_token()}",
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
                    "Authorization": f"Bearer {self._viewer_token()}",
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
                    "Authorization": f"Bearer {self._viewer_token()}",
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
                    "Authorization": f"Bearer {self._viewer_token()}",
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
                    "Authorization": f"Bearer {self._viewer_token()}",
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
                    "Authorization": f"Bearer {self._viewer_token()}",
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
                    "Authorization": f"Bearer {self._viewer_token()}",
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
                    "Authorization": f"Bearer {self._viewer_token()}",
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
                    "Authorization": f"Bearer {self._viewer_token()}",
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
                    "Authorization": f"Bearer {self._viewer_token()}",
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
        admin_token = self._admin_token()
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
            if resp.status_code in (301, 302, 303, 307, 308):
                # A valid token without the api_admin role (users.admin) is sent
                # to /login/denied; any other target is a proxy/URL redirect.
                location = resp.headers.get("Location", "")
                if "/login/denied" in location:
                    return False, (
                        "Netdisco denied the job: the admin token's user lacks the admin flag "
                        "(api_admin role) in Netdisco."
                    )
                if "/login" in location:
                    return False, "Netdisco sent the job request to its login page — it wasn't treated as an API call."
                return False, f"Netdisco redirected the job request to {location or 'an unknown location'} — check the configured URL."
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
        admin_token = self._admin_token()
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
        token = self._viewer_token()
        if not token:
            s.error = f"LENS_NETDISCO_VIEWER_TOKEN is not set — no token to authenticate against {url}."
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
                s.error = f"HTTP {status} from {url} — LENS_NETDISCO_VIEWER_TOKEN was rejected (missing, expired, or IP-restricted)."
            else:
                s.error = f"HTTP {status} from {url}."
        except Exception as e:
            s.error = str(e)
        return s
