from django.db import migrations, models


class Migration(migrations.Migration):

    initial = True

    dependencies = []

    operations = [
        migrations.CreateModel(
            name="Lens",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
            ],
            options={
                "default_permissions": (),
                "permissions": (
                    ("use_lens", "Can access LENS endpoint lookup"),
                    ("trigger_lens", "Can trigger Netdisco discovery jobs"),
                ),
            },
        ),
    ]
