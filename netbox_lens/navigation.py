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
                    link_text=_("Node History"),
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
