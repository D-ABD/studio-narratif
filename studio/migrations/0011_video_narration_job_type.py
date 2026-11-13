from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("studio", "0010_narrationsegment_animation_settings"),
    ]

    operations = [
        migrations.AlterField(
            model_name="narrationproject",
            name="job_type",
            field=models.CharField(
                choices=[
                    ("audio", "Narration audio"),
                    ("video", "Vidéo"),
                    ("assembly", "Assemblage de clips"),
                    ("video_narration", "Narration vidéo"),
                ],
                default="audio",
                max_length=16,
            ),
        ),
    ]
