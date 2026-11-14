import django.core.validators
from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion
import studio.models


class Migration(migrations.Migration):
    dependencies = [
        ("studio", "0014_backfill_owner_and_require_user"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="BackgroundTrack",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("name", models.CharField(max_length=120, verbose_name="nom")),
                ("audio_file", models.FileField(upload_to=studio.models.background_track_upload_to, verbose_name="fichier audio")),
                ("duration", models.FloatField(validators=[django.core.validators.MinValueValidator(0)], verbose_name="durée en secondes")),
                ("created_at", models.DateTimeField(default=studio.models.timezone.now)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("user", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="background_tracks", to=settings.AUTH_USER_MODEL)),
            ],
            options={"ordering": ["name", "id"]},
        ),
        migrations.AddField(
            model_name="narrationproject",
            name="background_track",
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="projects", to="studio.backgroundtrack"),
        ),
        migrations.AddField(
            model_name="narrationproject",
            name="music_volume",
            field=models.IntegerField(default=20, validators=[django.core.validators.MinValueValidator(0), django.core.validators.MaxValueValidator(100)]),
        ),
        migrations.AddConstraint(
            model_name="narrationproject",
            constraint=models.CheckConstraint(condition=models.Q(("music_volume__gte", 0), ("music_volume__lte", 100)), name="project_music_volume_0_100"),
        ),
    ]
