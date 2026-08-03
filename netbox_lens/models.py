from django.db import models


class Lens(models.Model):
    """Carries custom permissions for the LENS plugin. Not backed by a table.

    Named `Lens` (not e.g. `LensPermissions`) so its model_name is exactly "lens" —
    NetBox's permission resolver splits "netbox_lens.use_lens" as action="use",
    model="lens" (netbox/utilities/permissions.py:resolve_permission), so the model
    name must match the tail of the permission codename.
    """

    class Meta:
        managed = False
        default_permissions = ()
        permissions = (
            ("use_lens", "Can access LENS endpoint lookup"),
            ("trigger_lens", "Can trigger Netdisco discovery jobs"),
        )
