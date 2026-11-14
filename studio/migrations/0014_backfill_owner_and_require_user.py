# Assign all legacy projects and voices to the single existing superuser,
# then make ownership mandatory for all future records.
from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


def assign_legacy_records_to_single_superuser(apps, schema_editor):
    User = apps.get_model(*settings.AUTH_USER_MODEL.split("."))
    Voice = apps.get_model("studio", "Voice")
    NarrationProject = apps.get_model("studio", "NarrationProject")

    has_legacy_records = (
        Voice.objects.filter(user__isnull=True).exists()
        or NarrationProject.objects.filter(user__isnull=True).exists()
    )
    if not has_legacy_records:
        return

    superusers = User.objects.filter(is_superuser=True)
    count = superusers.count()
    if count == 0:
        raise RuntimeError(
            "Impossible d'attribuer les projets et voix existants : aucun superutilisateur "
            "n'existe. Crée le compte admin avant d'exécuter cette migration."
        )
    if count > 1:
        raise RuntimeError(
            f"Impossible d'attribuer les projets et voix existants : {count} superutilisateurs "
            "existent. La migration exige un compte admin unique."
        )

    owner_id = superusers.values_list("pk", flat=True).get()
    Voice.objects.filter(user__isnull=True).update(user_id=owner_id)
    NarrationProject.objects.filter(user__isnull=True).update(user_id=owner_id)


class Migration(migrations.Migration):
    dependencies = [
        ("studio", "0013_voice_narrationproject_user_nullable"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.RunPython(
            assign_legacy_records_to_single_superuser,
            migrations.RunPython.noop,
        ),
        migrations.AlterField(
            model_name="voice",
            name="user",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name="voices", to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AlterField(
            model_name="narrationproject",
            name="user",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name="narration_projects", to=settings.AUTH_USER_MODEL,
            ),
        ),
    ]
