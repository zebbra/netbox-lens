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


class LensBackend(ABC):
    name: str
    label: str
    icon: str = "mdi mdi-database-search"

    def __init__(self, config: dict):
        self.config = config

    @abstractmethod
    def search(self, query: str, archived: bool = False) -> SearchResult:
        ...
