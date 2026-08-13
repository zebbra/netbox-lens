import json
from abc import ABC, abstractmethod
from dataclasses import dataclass, field


@dataclass
class SearchResult:
    backend: str
    label: str
    icon: str = "mdi mdi-database-search"
    sightings: list = field(default_factory=list)
    ips: list = field(default_factory=list)
    macs: list = field(default_factory=list)
    devices: list = field(default_factory=list)
    error: str | None = None

    @property
    def has_results(self):
        return bool(self.sightings or self.ips or self.macs or self.devices)

    @property
    def sightings_json(self):
        return json.dumps(self.sightings, default=str)


@dataclass
class BackendStatus:
    backend: str
    label: str
    icon: str = "mdi mdi-database-search"
    stats: dict = field(default_factory=dict)
    error: str | None = None

    @property
    def ok(self):
        return self.error is None


class LensBackend(ABC):
    name: str
    label: str
    icon: str = "mdi mdi-database-search"

    def __init__(self, config: dict):
        self.config = config

    @abstractmethod
    def search(
        self, query: str, partial: bool = False, archived: bool = False,
        since: str | None = None, until: str | None = None,
    ) -> SearchResult:
        ...

    def device_nodes(self, device_ip: str, since: str | None = None, until: str | None = None) -> list:
        return []

    def device_summary(self, device_ip: str) -> dict:
        return {}

    def device_neighbors(self, device_ip: str) -> list:
        return []

    def device_ports(self, device_ip: str) -> list:
        return []

    def port_pae(self, device_ip: str, port: str) -> dict:
        return {}

    def search_ports(self, query: str, partial: bool = True) -> list:
        return []

    def node_sightings(
        self, query: str, partial: bool = False,
        since: str | None = None, until: str | None = None,
    ) -> list:
        return []

    def find_macs(self, query: str, partial: bool = True) -> list:
        return []

    def arp_entries(
        self, query: str, partial: bool = False,
        since: str | None = None, until: str | None = None,
    ) -> list:
        return []

    def resolve_mac(self, mac: str) -> dict | None:
        return None

    def device_web_url(self, device_ip: str) -> str | None:
        return None

    def trigger_discover(self, device_ip: str) -> tuple[bool, str]:
        return False, f"{self.label} does not support triggering discovery."

    def trigger_macsuck(self, device_ip: str) -> tuple[bool, str]:
        return False, f"{self.label} does not support triggering macsuck."

    def trigger_arpnip(self, device_ip: str) -> tuple[bool, str]:
        return False, f"{self.label} does not support triggering arpnip."

    def status(self) -> BackendStatus:
        return BackendStatus(backend=self.name, label=self.label, icon=self.icon)
