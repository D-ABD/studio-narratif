from django.db import migrations


def reset_projects_to_xtts(apps, schema_editor):
    NarrationProject = apps.get_model("studio", "NarrationProject")
    NarrationProject.objects.using(schema_editor.connection.alias).all().update(
        imported_audio=None,
        imported_audio_cache_key="",
        imported_audio_alignment={},
        audio_warning="",
        audio_source="xtts",
    )


class Migration(migrations.Migration):
    dependencies = [
        ("studio", "0017_imported_audio_alignment_cache"),
    ]

    operations = [
        migrations.RunPython(reset_projects_to_xtts, migrations.RunPython.noop),
        migrations.RemoveField(
            model_name="narrationproject",
            name="imported_audio",
        ),
        migrations.RemoveField(
            model_name="narrationproject",
            name="imported_audio_cache_key",
        ),
        migrations.RemoveField(
            model_name="narrationproject",
            name="imported_audio_alignment",
        ),
        migrations.RemoveField(
            model_name="narrationproject",
            name="audio_warning",
        ),
        migrations.RemoveField(
            model_name="narrationproject",
            name="audio_source",
        ),
    ]
