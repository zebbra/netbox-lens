from django.utils.translation import gettext_lazy as _
from netbox.plugins import PluginMenu, PluginMenuItem

menu = PluginMenu(
    label=_("LENS"),
    icon_class="mdi mdi-magnify-scan",
    groups=(
        (
            _("Lookup"),
            (
                PluginMenuItem(
                    link="plugins:netbox_lens:search",
                    link_text=_("Endpoint"),
                    permissions=["netbox_lens.use_lens"],
                ),
            ),
        ),
        (
            _("Legacy"),
            (
                PluginMenuItem(
                    link="plugins:netbox_lens:mac_history",
                    link_text=_("MAC History"),
                    permissions=["netbox_lens.use_lens"],
                ),
                PluginMenuItem(
                    link="plugins:netbox_lens:arp_history",
                    link_text=_("ARP History"),
                    permissions=["netbox_lens.use_lens"],
                ),
                PluginMenuItem(
                    link="plugins:netbox_lens:interface_search",
                    link_text=_("Default"),
                    permissions=["netbox_lens.use_lens"],
                ),
            ),
        ),
        (
            _("Status"),
            (
                PluginMenuItem(
                    link="plugins:netbox_lens:status",
                    link_text=_("Backend"),
                    permissions=["netbox_lens.use_lens"],
                ),
            ),
        ),
    ),
)
