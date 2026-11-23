from django.db import migrations, models
import studio.models


def _settings_for_legacy_motion(motion):
    effects = {
        "zoom": {"enabled": False, "level": "medium", "direction": "in"},
        "pan": {"enabled": False, "level": "medium", "direction": "horizontal"},
        "parallax": {"enabled": False, "level": "medium"},
        "handheld": {"enabled": False, "level": "medium"},
        "light_breathing": {"enabled": False, "level": "medium"},
        "light_leak": {"enabled": False, "level": "medium"},
    }
    if motion == "zoom_in":
        effects["zoom"].update(enabled=True, direction="in")
    elif motion == "zoom_out":
        effects["zoom"].update(enabled=True, direction="out")
    elif motion == "pan_left":
        effects["pan"].update(enabled=True, direction="horizontal_reverse")
    elif motion == "pan_right":
        effects["pan"].update(enabled=True, direction="horizontal")
    elif motion == "parallax":
        effects["parallax"]["enabled"] = True
    return {"version": 1, "camera_profile": "linear", "effects": effects}


def migrate_legacy_assembly_motion(apps, schema_editor):
    NarrationSegment = apps.get_model("studio", "NarrationSegment")
    database = schema_editor.connection.alias
    segments = NarrationSegment.objects.using(database).only("id", "assembly_motion")
    for segment in segments.iterator(chunk_size=500):
        segment.assembly_animation_settings = _settings_for_legacy_motion(segment.assembly_motion)
        segment.save(update_fields=["assembly_animation_settings"], using=database)


class Migration(migrations.Migration):
    dependencies = [
        ("studio", "0027_narrationsegment_assembly_motion"),
    ]

    operations = [
        migrations.AddField(
            model_name="narrationsegment",
            name="assembly_animation_settings",
            field=models.JSONField(blank=True, default=studio.models.default_assembly_animation_settings),
        ),
        migrations.RunPython(migrate_legacy_assembly_motion, migrations.RunPython.noop),
    ]
