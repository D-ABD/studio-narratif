import django.core.validators
from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion
import studio.models


def set_existing_projects_to_xtts(apps, schema_editor):
    """Keep every project that predates audio imports on the original XTTS path."""
    NarrationProject = apps.get_model("studio", "NarrationProject")
    NarrationProject.objects.using(schema_editor.connection.alias).update(audio_source="xtts")


class Migration(migrations.Migration):
    dependencies = [
        ("studio", "0015_background_music"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="AudioAsset",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("name", models.CharField(max_length=160, verbose_name="nom")),
                ("audio_file", models.FileField(upload_to=studio.models.audio_asset_upload_to, verbose_name="fichier audio")),
                ("duration", models.FloatField(validators=[django.core.validators.MinValueValidator(0)], verbose_name="durée en secondes")),
                ("source", models.CharField(choices=[("upload", "Importé"), ("xtts", "Généré par XTTS")], default="upload", max_length=8)),
                ("text", models.TextField(blank=True, null=True)),
                ("created_at", models.DateTimeField(default=studio.models.timezone.now)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("project", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="audio_assets", to="studio.narrationproject")),
                ("user", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="audio_assets", to=settings.AUTH_USER_MODEL)),
                ("voice", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="audio_assets", to="studio.voice")),
            ],
            options={"ordering": ["-created_at", "-id"]},
        ),
        migrations.AddField(
            model_name="narrationproject",
            name="audio_source",
            field=models.CharField(choices=[("xtts", "Voix générée par XTTS"), ("imported", "Audio importé")], default="xtts", max_length=8),
        ),
        migrations.AddField(
            model_name="narrationproject",
            name="imported_audio",
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="imported_by_projects", to="studio.audioasset"),
        ),
        migrations.RunPython(set_existing_projects_to_xtts, migrations.RunPython.noop),
    ]
