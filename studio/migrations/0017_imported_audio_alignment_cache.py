from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("studio", "0016_audioasset_audio_source"),
    ]

    operations = [
        migrations.AddField(
            model_name="narrationproject",
            name="imported_audio_cache_key",
            field=models.CharField(blank=True, max_length=64),
        ),
        migrations.AddField(
            model_name="narrationproject",
            name="imported_audio_alignment",
            field=models.JSONField(blank=True, default=dict),
        ),
        migrations.AddField(
            model_name="narrationproject",
            name="audio_warning",
            field=models.TextField(blank=True),
        ),
    ]
