from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("netbox_lens", "0001_initial"),
    ]

    operations = [
        migrations.AlterModelOptions(
            name="lens",
            options={
                "default_permissions": (),
                "permissions": (
                    ("use_lens", "Can access LENS endpoint lookup"),
                    ("trigger_lens", "Can trigger Netdisco discovery jobs"),
                    ("jobs_lens", "Can view a device's Netdisco job queue"),
                ),
            },
        ),
    ]
