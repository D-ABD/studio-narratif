from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("studio", "0006_alter_narrationsegment_motion"),
    ]

    operations = [
        migrations.AddField(
            model_name="narrationsegment",
            name="video_clip",
            field=models.FileField(blank=True, upload_to="scenes/videos/"),
        ),
        migrations.AlterField(
            model_name="narrationproject",
            name="job_type",
            field=models.CharField(
                choices=[
                    ("audio", "Narration audio"),
                    ("video", "Vidéo"),
                    ("assembly", "Assemblage de clips"),
                ],
                default="audio",
                max_length=8,
            ),
        ),
    ]
