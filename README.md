# netbox-lens

**LENS** — Locate Endpoints across Network Systems

A [NetBox](https://netbox.dev) plugin for unified endpoint lookup across multiple network data sources. Search by MAC address, IP address, or hostname and get port sightings, IP assignments, and resolved MACs back in a single view — regardless of which backend system holds the data.

## Features

- Search by MAC, IP, or hostname from a dedicated NetBox page
- Pluggable backend architecture — add new data sources by implementing a single class
- Parallel queries across all configured backends
- Built-in [Netdisco](https://netdisco.org) backend
- HTMX-powered inline results (no full page reload)

## Installation

```bash
pip install netbox-lens
```

Add to your NetBox `configuration.py`:

```python
PLUGINS = [
    "netbox_lens",
    # ...
]

PLUGINS_CONFIG = {
    "netbox_lens": {
        "backends": {
            "netdisco": {
                "url": "https://netdisco.example.com",
                # token via env var LENS_NETDISCO_TOKEN (recommended)
                # or inline: "token": "your-token-here"
            }
        }
    }
}
```

Set the Netdisco API token as an environment variable:

```bash
export LENS_NETDISCO_TOKEN=your-long-lived-token
```

## Backends

### Netdisco

Queries the [`/api/v1/search/node`](https://metacpan.org/pod/App::Netdisco) endpoint. Requires a long-lived API token (`Authorization: Bearer <token>`).

| Config key    | Default | Description                        |
|---------------|---------|------------------------------------|
| `url`         | —       | Base URL of your Netdisco instance |
| `token`       | —       | API token (prefer env var instead) |
| `verify_ssl`  | `true`  | Verify TLS certificate             |
| `timeout`     | `15`    | Request timeout in seconds         |

### Adding a backend

Implement `LensBackend` and register it:

```python
from netbox_lens.backends.base import LensBackend, SearchResult

class MyBackend(LensBackend):
    name = "mybackend"
    label = "My Backend"

    def search(self, query: str, archived: bool = False) -> SearchResult:
        result = SearchResult(backend=self.name, label=self.label, icon=self.icon)
        # ... populate result.sightings / result.ips / result.macs
        return result
```

Then add it to the registry in `netbox_lens/backends/__init__.py` and configure it under `PLUGINS_CONFIG["netbox_lens"]["backends"]`.

## Requirements

- NetBox 4.0+
- Python 3.12+

## License

Apache 2.0 — see [LICENSE](LICENSE).
