from django.db import migrations, models


def backfill_assembly_motion(apps, schema_editor):
    NarrationSegment = apps.get_model("studio", "NarrationSegment")
    NarrationSegment.objects.using(schema_editor.connection.alias).update(assembly_motion="none")


class Migration(migrations.Migration):

    dependencies = [
        ("studio", "0026_mediafolder_audioasset_folder_backgroundtrack_folder"),
    ]

    operations = [
        migrations.AddField(
            model_name="narrationsegment",
            name="assembly_motion",
            field=models.CharField(
                choices=[
                    ("none", "Aucun"),
                    ("zoom_in", "Zoom avant"),
                    ("zoom_out", "Zoom arrière"),
                    ("pan_left", "Panoramique gauche"),
                    ("pan_right", "Panoramique droit"),
                    ("parallax", "Parallaxe"),
                ],
                default="none",
                max_length=12,
            ),
        ),
        migrations.RunPython(backfill_assembly_motion, migrations.RunPython.noop),
    ]
