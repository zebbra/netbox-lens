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
    error: str | None = None

    @property
    def has_results(self):
        return bool(self.sightings or self.ips or self.macs)

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
    def search(self, query: str, partial: bool = False, archived: bool = False, since: str | None = None) -> SearchResult:
        ...

    def device_nodes(self, device_ip: str) -> list:
        return []

    def status(self) -> BackendStatus:
        return BackendStatus(backend=self.name, label=self.label, icon=self.icon)
