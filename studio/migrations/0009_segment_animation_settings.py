from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("studio", "0008_merge_video_clip_assembly"),
    ]

    operations = [
        migrations.AddField(
            model_name="narrationsegment",
            name="camera_profile",
            field=models.CharField(
                choices=[("linear", "Linéaire"), ("organic", "Organique")],
                default="linear",
                max_length=8,
            ),
        ),
        migrations.AddField(
            model_name="narrationsegment",
            name="light_breathing",
            field=models.BooleanField(default=False),
        ),
    ]
