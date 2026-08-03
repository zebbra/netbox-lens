from netbox.plugins import PluginConfig


class LensConfig(PluginConfig):
    name = "netbox_lens"
    verbose_name = "LENS"
    description = "Locate Endpoints across Network Systems"
    version = "1.0.2"
    base_url = "lens"
    min_version = "4.0.0"
    default_settings = {
        "backends": {},
    }


config = LensConfig
