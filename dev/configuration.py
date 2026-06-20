import os

ALLOWED_HOSTS = ["*"]

DATABASE = {
    "ENGINE": "django.db.backends.postgresql",
    "NAME": "netbox",
    "USER": "netbox",
    "PASSWORD": "netbox",
    "HOST": "postgres",
    "PORT": "5432",
}

REDIS = {
    "tasks": {"HOST": "redis", "PORT": 6379, "DATABASE": 0, "SSL": False},
    "caching": {"HOST": "redis", "PORT": 6379, "DATABASE": 1, "SSL": False},
}

SECRET_KEY = "dev-only-secret-key-not-for-production-use"

PLUGINS = ["netbox_lens"]

PLUGINS_CONFIG = {
    "netbox_lens": {
        "backends": {
            "netdisco": {
                "url": os.environ.get("NETDISCO_URL", "http://netdisco-web:5000"),
                # token via LENS_NETDISCO_TOKEN env var
            }
        }
    }
}
