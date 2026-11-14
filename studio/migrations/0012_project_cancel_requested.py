from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("studio", "0011_video_narration_job_type"),
    ]

    operations = [
        migrations.AddField(
            model_name="narrationproject",
            name="cancel_requested",
            field=models.BooleanField(default=False),
        ),
    ]
