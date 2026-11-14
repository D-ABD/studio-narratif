# Generated manually for user ownership.
from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("studio", "0012_project_cancel_requested"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name="voice",
            name="user",
            field=models.ForeignKey(
                null=True, blank=True, on_delete=django.db.models.deletion.PROTECT,
                related_name="voices", to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AddField(
            model_name="narrationproject",
            name="user",
            field=models.ForeignKey(
                null=True, blank=True, on_delete=django.db.models.deletion.PROTECT,
                related_name="narration_projects", to=settings.AUTH_USER_MODEL,
            ),
        ),
    ]
