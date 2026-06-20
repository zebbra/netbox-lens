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
                    link_text=_("Endpoint Lookup"),
                ),
            ),
        ),
    ),
)
