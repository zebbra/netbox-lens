import os

import requests

from .base import LensBackend, SearchResult


class NetdiscoBackend(LensBackend):
    name = "netdisco"
    label = "Netdisco"
    icon = "mdi mdi-network"

    def search(self, query: str, archived: bool = False) -> SearchResult:
        result = SearchResult(backend=self.name, label=self.label, icon=self.icon)

        base_url = self.config.get("url", "").rstrip("/")
        if not base_url:
            result.error = "Netdisco URL is not configured."
            return result

        try:
            resp = requests.get(
                f"{base_url}/api/v1/search/node",
                headers={"Authorization": f"Bearer {os.environ.get('LENS_NETDISCO_TOKEN', self.config.get('token', ''))}"},
                params={
                    "q": query,
                    "partial": "false",
                    "deviceports": "true",
                    "show_vendor": "true",
                    "archived": "true" if archived else "false",
                },
                timeout=self.config.get("timeout", 15),
                verify=self.config.get("verify_ssl", True),
            )
            resp.raise_for_status()
            data = resp.json()
            result.sightings = data.get("sightings") or []
            result.ips = data.get("ips") or []
            result.macs = data.get("macs") or []
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
