from netbox.plugins import PluginConfig


class LensConfig(PluginConfig):
    name = "netbox_lens"
    verbose_name = "LENS"
    description = "Locate Endpoints across Network Systems"
    version = "1.0.30"
    author = "Stefan Grosser"
    author_email = "stefan.grosser@zebbra.ch"
    base_url = "lens"
    min_version = "4.0.0"
    default_settings = {
        "backends": {},
        # Device role slugs that get the LENS panel on the device page —
        # infrastructure only, not endpoints like APs (lwapp-ap).
        "device_panel_roles": ["router", "firewall", "lwapp-ctr", "switch"],
        # Object CF (-> dcim.device) that discobox's WLC sync sets on every AP
        # to point at its controller; the WLC panel counts APs by it.
        "wlc_controller_cf": "controller",
    }


config = LensConfig
