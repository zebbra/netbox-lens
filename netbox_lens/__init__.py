from netbox.plugins import PluginConfig


class LensConfig(PluginConfig):
    name = "netbox_lens"
    verbose_name = "LENS"
    description = "Locate Endpoints across Network Systems"
    version = "1.0.43"
    author = "Stefan Grosser"
    author_email = "stefan.grosser@zebbra.ch"
    base_url = "lens"
    min_version = "4.0.0"
    default_settings = {
        "backends": {},
        # Device role slugs that get the LENS panel on the device page —
        # infrastructure only, not endpoints like APs (lwapp-ap).
        "device_panel_roles": ["router", "firewall", "border-firewall", "lwapp-ctr", "switch"],
        # Object CF (-> dcim.device) that discobox's WLC sync sets on every AP
        # to point at its controller; the WLC panel counts APs by it.
        "wlc_controller_cf": "controller",
        # Device role slugs of APs: a MAC History / search row whose MAC is
        # one of their interfaces shows as "AP" in the Connection column.
        "ap_roles": ["lwapp-ap"],
        # Device role slugs Refresh IP never touches (single and bulk): APs
        # get their address from DHCP, a DNS/Netdisco lookup isn't theirs.
        "refresh_ip_exclude_roles": ["lwapp-ap"],
        # Time zone of timestamps that arrive without one (Netdisco's are UTC).
        "naive_timezone": "UTC",
    }


config = LensConfig
