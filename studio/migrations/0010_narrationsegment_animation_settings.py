from django.db import migrations, models

from studio.models import default_animation_settings


def copy_legacy_animation_settings(apps, schema_editor):
    NarrationSegment = apps.get_model("studio", "NarrationSegment")
    database = schema_editor.connection.alias
    for segment in NarrationSegment.objects.using(database).all().iterator():
        settings = {
            "version": 1,
            "camera_profile": segment.camera_profile if segment.camera_profile in {"linear", "organic"} else "linear",
            "effects": {
                "zoom": {"enabled": False, "level": "medium", "direction": "in"},
                "pan": {"enabled": False, "level": "medium", "direction": "horizontal"},
                "parallax": {"enabled": False, "level": "medium"},
                "handheld": {"enabled": False, "level": "medium"},
                "light_breathing": {"enabled": bool(segment.light_breathing), "level": "medium"},
                "light_leak": {"enabled": False, "level": "medium"},
            },
        }
        effects = settings["effects"]
        if segment.motion == "zoom_in":
            effects["zoom"]["enabled"] = True
        elif segment.motion == "zoom_out":
            effects["zoom"].update(enabled=True, direction="out")
        elif segment.motion == "pan_h":
            effects["pan"].update(enabled=True, direction="horizontal")
        elif segment.motion == "pan_v":
            effects["pan"].update(enabled=True, direction="vertical")
        elif segment.motion == "zoom_pan":
            effects["zoom"]["enabled"] = True
            effects["pan"].update(enabled=True, direction="diagonal")
        elif segment.motion == "parallax":
            effects["parallax"]["enabled"] = True
        segment.animation_settings = settings
        segment.save(update_fields=["animation_settings"], using=database)


class Migration(migrations.Migration):

    dependencies = [
        ("studio", "0009_segment_animation_settings"),
    ]

    operations = [
        migrations.AddField(
            model_name="narrationsegment",
            name="animation_settings",
            field=models.JSONField(blank=True, default=default_animation_settings),
        ),
        migrations.RunPython(
            copy_legacy_animation_settings,
            reverse_code=migrations.RunPython.noop,
        ),
    ]
