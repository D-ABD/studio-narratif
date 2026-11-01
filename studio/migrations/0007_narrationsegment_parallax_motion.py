from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("studio", "0006_alter_narrationsegment_motion"),
    ]

    operations = [
        migrations.AlterField(
            model_name="narrationsegment",
            name="motion",
            field=models.CharField(
                choices=[
                    ("still", "Image fixe"),
                    ("zoom_in", "Zoom avant lent"),
                    ("zoom_out", "Zoom arrière lent"),
                    ("pan_h", "Panoramique gauche → droite"),
                    ("pan_v", "Panoramique haut → bas"),
                    ("zoom_pan", "Zoom diagonal doux"),
                    ("parallax", "Profondeur douce · parallaxe 2.5D"),
                ],
                default="zoom_in",
                max_length=8,
            ),
        ),
    ]
