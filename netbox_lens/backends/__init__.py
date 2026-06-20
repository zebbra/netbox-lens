from .base import LensBackend, SearchResult
from .netdisco import NetdiscoBackend

REGISTRY: dict[str, type[LensBackend]] = {
    "netdisco": NetdiscoBackend,
}


def get_backends(config: dict) -> list[LensBackend]:
    backends_config = config.get("backends", {})
    return [
        REGISTRY[name](backend_config)
        for name, backend_config in backends_config.items()
        if name in REGISTRY and backend_config.get("enabled", True)
    ]
