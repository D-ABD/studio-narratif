from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("studio", "0022_audio_session"),
    ]

    operations = [
        migrations.AddField(
            model_name="audiosession",
            name="operations_version",
            field=models.PositiveIntegerField(default=0),
        ),
    ]
