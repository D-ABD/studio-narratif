# Merge two migration branches created from the same 0006 migration.
from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("studio", "0007_narrationsegment_parallax_motion"),
        ("studio", "0007_video_clip_assembly"),
    ]

    operations = []
