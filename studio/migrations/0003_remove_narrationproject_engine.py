from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("studio", "0002_narrationproject_engine"),
    ]

    operations = [
        migrations.RemoveField(
            model_name="narrationproject",
            name="engine",
        ),
    ]
