from django.db import models


class Lens(models.Model):
    """Carries custom permissions for the LENS plugin. Never actually written to —
    it exists only so NetBox has a model to hang the use_lens/trigger_lens
    permissions off of.

    Named `Lens` (not e.g. `LensPermissions`) so its model_name is exactly "lens" —
    NetBox's permission resolver splits "netbox_lens.use_lens" as action="use",
    model="lens" (netbox/utilities/permissions.py:resolve_permission), so the model
    name must match the tail of the permission codename.

    Deliberately managed (has a real, empty table) rather than managed=False:
    any plugin model becomes ObjectType.public=True automatically (see
    netbox.models.features.model_is_public), and NetBox's own /core/system
    page counts every public model's rows via model.objects.count() with no
    check for whether it's managed. An unmanaged model with public=True
    crashes that page with ProgrammingError (relation does not exist) —
    marking it managed avoids that without losing Permission-page visibility,
    which also requires public=True.
    """

    class Meta:
        default_permissions = ()
        permissions = (
            ("use_lens", "Can access LENS endpoint lookup"),
            ("trigger_lens", "Can trigger Netdisco discovery jobs"),
        )
