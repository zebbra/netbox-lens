import os

PLUGINS = ["netbox_lens"]

PLUGINS_CONFIG = {
    "netbox_lens": {
        "backends": {
            "netdisco": {
                "url": os.environ.get("NETDISCO_URL", "http://netdisco-web:5000"),
            }
        }
    }
}
