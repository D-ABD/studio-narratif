import json
from io import BytesIO
from pathlib import Path

from django.contrib import messages
from django.core.files.base import ContentFile
from django.db import transaction
from django.http import HttpResponse, HttpResponseBadRequest, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST
from PIL import Image, UnidentifiedImageError

from .models import NarrationProject, NarrationSegment, Voice
from .services import _voice_sample_digest, cache_key_for, hash_file, normalize_voice_sample, srt_text

MAX_SAMPLE_BYTES = 25 * 1024 * 1024
ALLOWED_SAMPLE_EXTENSIONS = {".wav", ".mp3", ".webm", ".m4a", ".ogg"}


def _invalidate_export(project):
    if project.full_audio:
        project.full_audio.delete(save=False)
    if project.subtitles:
        project.subtitles.delete(save=False)
    if project.video_file:
        project.video_file.delete(save=False)
    project.full_audio = ""
    project.subtitles = ""
    project.video_file = ""
    project.duration_seconds = 0
    project.status = NarrationProject.Status.DRAFT
    project.queued_at = None
    project.job_type = NarrationProject.JobType.AUDIO
    project.status_message = "Modifications en attente de préparation."
    project.error_message = ""
    project.progress_percent = 0
    project.started_at = None
    project.finished_at = None
    project.save(update_fields=[
        "voice", "full_audio", "subtitles", "video_file", "duration_seconds", "status", "status_message", "error_message",
        "progress_percent", "queued_at", "job_type", "started_at", "finished_at", "updated_at",
    ])


def _invalidate_video(project):
    update_fields = ["updated_at"]
    if project.video_file:
        project.video_file.delete(save=False)
        project.video_file = ""
        update_fields.append("video_file")
    if project.status == NarrationProject.Status.READY:
        project.status_message = "Les réglages vidéo ont changé. Génère une nouvelle vidéo."
        update_fields.append("status_message")
    project.save(update_fields=update_fields)


def _scene_images(upload):
    name = Path(upload.name).name
    suffix = Path(name).suffix.lower()
    if upload.size > 25 * 1024 * 1024:
        raise ValueError(f"« {name} » dépasse la limite de 25 Mo.")
    raw = upload.read()
    if suffix == ".pdf":
        try:
            import pymupdf
            document = pymupdf.open(stream=raw, filetype="pdf")
            if document.is_encrypted:
                raise ValueError(f"Le PDF « {name} » est protégé par mot de passe.")
            if document.page_count > 30:
                raise ValueError(f"Le PDF « {name} » dépasse la limite de 30 pages par import.")
            output = []
            total_pixels = 0
            for index, page in enumerate(document, start=1):
                pixmap = page.get_pixmap(matrix=pymupdf.Matrix(1.4, 1.4), alpha=False)
                total_pixels += pixmap.width * pixmap.height
                if pixmap.width * pixmap.height > 24_000_000 or total_pixels > 100_000_000:
                    raise ValueError(f"La page {index} du PDF « {name} » a une résolution trop élevée.")
                output.append((f"{Path(name).stem}-page-{index:03}.png", pixmap.tobytes("png")))
            document.close()
            if not output:
                raise ValueError(f"Le PDF « {name} » ne contient aucune page.")
            return output
        except ValueError:
            raise
        except Exception as exc:
            raise ValueError(f"Impossible de lire le PDF « {name} » : {exc}") from exc
    if suffix not in {".jpg", ".jpeg", ".png"}:
        raise ValueError(f"Format non accepté pour « {name} » : utilise JPG, PNG ou PDF.")
    try:
        with Image.open(BytesIO(raw)) as image:
            image.verify()
        with Image.open(BytesIO(raw)) as image:
            if image.width * image.height > 40_000_000:
                raise ValueError(f"L’image « {name} » a une résolution trop élevée.")
    except UnidentifiedImageError as exc:
        raise ValueError(f"Le fichier « {name} » n’est pas une image lisible.") from exc
    except OSError as exc:
        raise ValueError(f"Impossible de lire l’image « {name} » : {exc}") from exc
    return [(name, raw)]


def home(request):
    query = (request.GET.get("q") or "").strip()[:80]
    category = request.GET.get("category", "")
    voices = Voice.objects.all()
    if query:
        voices = voices.filter(name__icontains=query)
    if category in Voice.Category.values:
        voices = voices.filter(category=category)
    return render(request, "studio/home.html", {
        "voices": voices,
        "projects": NarrationProject.objects.all()[:30],
        "voice_count": Voice.objects.count(),
        "project_count": NarrationProject.objects.count(),
        "query": query,
        "selected_category": category,
        "categories": Voice.Category.choices,
    })


@require_POST
def create_voice(request):
    upload = request.FILES.get("sample")
    name = (request.POST.get("name") or "").strip()[:80]
    category = request.POST.get("category", Voice.Category.OTHER)
    if not name:
        messages.error(request, "Donne un nom à cette voix.")
        return redirect("home")
    if category not in Voice.Category.values:
        messages.error(request, "Choisis une catégorie proposée.")
        return redirect("home")
    if request.POST.get("voice_consent") != "on":
        messages.error(request, "Confirme que tu es autorisé·e à cloner et utiliser cette voix.")
        return redirect("home")
    if not upload or upload.size == 0:
        messages.error(request, "Sélectionne un échantillon audio ou enregistre ta voix.")
        return redirect("home")
    if upload.size > MAX_SAMPLE_BYTES:
        messages.error(request, "L’échantillon dépasse 25 Mo.")
        return redirect("home")
    if Path(upload.name).suffix.lower() not in ALLOWED_SAMPLE_EXTENSIONS:
        messages.error(request, "Formats acceptés : WAV, MP3 ou enregistrement navigateur.")
        return redirect("home")
    try:
        wav_bytes = normalize_voice_sample(upload)
        voice = Voice(name=name, category=category, consent_confirmed=True)
        voice.sample.save(f"voice-{timezone.now():%Y%m%d-%H%M%S}.wav", ContentFile(wav_bytes), save=False)
        voice.sample_sha256 = hash_file(voice.sample.path) if voice.sample.name else ""
        voice.save()
        messages.success(request, f"La voix « {voice.name} » est enregistrée sur cet appareil.")
    except Exception as exc:
        messages.error(request, str(exc) or "Impossible d’enregistrer cet échantillon audio.")
    return redirect("home")


@require_POST
def edit_voice(request, voice_id):
    voice = get_object_or_404(Voice, pk=voice_id)
    name = (request.POST.get("name") or "").strip()[:80]
    category = request.POST.get("category", "")
    if not name or category not in Voice.Category.values:
        messages.error(request, "Le nom ou la catégorie de la voix est invalide.")
    else:
        voice.name = name
        voice.category = category
        voice.save(update_fields=["name", "category", "updated_at"])
        messages.success(request, "La voix a été mise à jour.")
    return redirect("home")


@require_POST
def delete_voice(request, voice_id):
    voice = get_object_or_404(Voice, pk=voice_id)
    if voice.projects.filter(status__in=[NarrationProject.Status.QUEUED, NarrationProject.Status.PROCESSING]).exists():
        messages.error(request, "Cette voix est utilisée par un traitement en attente ou en cours.")
        return redirect("home")
    if request.POST.get("confirm") != "yes":
        messages.error(request, "La suppression n’a pas été confirmée.")
        return redirect("home")
    name = voice.name
    sample = voice.sample
    voice.delete()
    if sample:
        sample.delete(save=False)
    messages.success(request, f"La voix « {name} » a été supprimée.")
    return redirect("home")


@require_POST
def create_project(request):
    title = (request.POST.get("title") or "Nouvelle narration").strip()[:120] or "Nouvelle narration"
    project = NarrationProject.objects.create(title=title)
    NarrationSegment.objects.create(project=project, position=0)
    return redirect("project_detail", project_id=project.pk)


def project_detail(request, project_id):
    project = get_object_or_404(NarrationProject, pk=project_id)
    segments = list(project.segments.all())
    voice = project.voice if project.voice_id else None
    try:
        sample_digest = _voice_sample_digest(voice) if voice else ""
    except (OSError, ValueError):
        sample_digest = ""
    for segment in segments:
        try:
            segment.ready_for_voice = bool(
                voice and sample_digest and segment.text.strip()
                and segment.cache_key == cache_key_for(segment.text, voice, sample_digest=sample_digest)
                and segment.audio and Path(segment.audio.path).is_file()
                and segment.subtitle_data
            )
        except (OSError, ValueError):
            segment.ready_for_voice = False
    return render(request, "studio/project.html", {
        "project": project,
        "projects": NarrationProject.objects.all()[:30],
        "segments": segments,
        "voices": Voice.objects.all(),
        "categories": Voice.Category.choices,
        "video_formats": NarrationProject.VideoFormat.choices,
        "motions": NarrationSegment.Motion.choices,
        "has_scenes": bool(segments and any(segment.image for segment in segments)),
    })


@require_POST
def import_scenes(request, project_id):
    project = get_object_or_404(NarrationProject, pk=project_id)
    if project.status in {NarrationProject.Status.QUEUED, NarrationProject.Status.PROCESSING}:
        return JsonResponse({"ok": False, "error": "Attends la fin du traitement avant d’ajouter des scènes."}, status=409)
    uploads = request.FILES.getlist("files")
    if not uploads:
        messages.error(request, "Sélectionne au moins une image ou un PDF.")
        return redirect("project_detail", project_id=project.pk)
    try:
        scenes = []
        for upload in uploads:
            scenes.extend(_scene_images(upload))
            if len(scenes) > 30:
                raise ValueError("Un import est limité à 30 scènes.")
    except ValueError as exc:
        messages.error(request, str(exc))
        return redirect("project_detail", project_id=project.pk)

    existing = list(project.segments.order_by("position", "id"))
    reusable = next((item for item in existing if not item.image and not item.text.strip()), None)
    if reusable is None:
        reusable = next((item for item in existing if not item.image), None)
    for filename, content in scenes:
        segment = reusable
        if reusable:
            reusable = None
        else:
            segment = NarrationSegment.objects.create(project=project, position=project.segments.count())
        if segment.image:
            segment.image.delete(save=False)
        segment.image.save(filename, ContentFile(content), save=False)
        segment.save(update_fields=["image", "updated_at"])
    _invalidate_export(project)
    messages.success(request, f"{len(scenes)} scène(s) ajoutée(s) au projet.")
    return redirect("project_detail", project_id=project.pk)


@require_POST
def save_video_options(request, project_id):
    project = get_object_or_404(NarrationProject, pk=project_id)
    if project.status in {NarrationProject.Status.QUEUED, NarrationProject.Status.PROCESSING}:
        return JsonResponse({"ok": False, "error": "Attends la fin du traitement avant de modifier les réglages."}, status=409)
    video_format = request.POST.get("video_format")
    if video_format not in NarrationProject.VideoFormat.values:
        messages.error(request, "Choisis le format vertical ou horizontal.")
        return redirect("project_detail", project_id=project.pk)
    subtitles_enabled = request.POST.get("subtitles_enabled") == "on"
    if (project.video_format, project.subtitles_enabled) != (video_format, subtitles_enabled):
        project.video_format = video_format
        project.subtitles_enabled = subtitles_enabled
        project.save(update_fields=["video_format", "subtitles_enabled", "updated_at"])
        _invalidate_video(project)
        messages.success(request, "Les réglages vidéo ont été enregistrés.")
    return redirect("project_detail", project_id=project.pk)


@require_POST
def set_project_voice(request, project_id):
    project = get_object_or_404(NarrationProject, pk=project_id)
    voice_id = request.POST.get("voice_id")
    voice = get_object_or_404(Voice, pk=voice_id, consent_confirmed=True) if voice_id else None
    if project.status in {NarrationProject.Status.QUEUED, NarrationProject.Status.PROCESSING}:
        return JsonResponse({"ok": False, "error": "Ce projet est déjà dans la file ou en cours de traitement."}, status=409)
    if project.voice_id != (voice.pk if voice else None):
        project.voice = voice
        _invalidate_export(project)
    return redirect("project_detail", project_id=project.pk)


@require_POST
def add_segment(request, project_id):
    project = get_object_or_404(NarrationProject, pk=project_id)
    if project.status in {NarrationProject.Status.QUEUED, NarrationProject.Status.PROCESSING}:
        return JsonResponse({"ok": False, "error": "Attends la fin du traitement avant de modifier le projet."}, status=409)
    NarrationSegment.objects.create(project=project, position=project.segments.count())
    _invalidate_export(project)
    return redirect("project_detail", project_id=project.pk)


@require_POST
def save_segment(request, project_id, segment_id):
    project = get_object_or_404(NarrationProject, pk=project_id)
    segment = get_object_or_404(NarrationSegment, pk=segment_id, project=project)
    if project.status in {NarrationProject.Status.QUEUED, NarrationProject.Status.PROCESSING}:
        return JsonResponse({"ok": False, "error": "Attends la fin du traitement avant de modifier le projet."}, status=409)
    text = (request.POST.get("text") or "").strip()[:12000]
    motion = request.POST.get("motion", segment.motion)
    if motion not in NarrationSegment.Motion.values:
        return HttpResponseBadRequest("Mouvement de scène invalide.")
    text_changed = text != segment.text
    motion_changed = motion != segment.motion
    if text_changed:
        if segment.audio:
            segment.audio.delete(save=False)
        segment.text = text
        segment.audio = ""
        segment.subtitle_data = []
        segment.duration_seconds = 0
        segment.cache_key = ""
    segment.text = text
    segment.motion = motion
    segment.save(update_fields=["text", "motion", "audio", "subtitle_data", "duration_seconds", "cache_key", "updated_at"])
    if text_changed:
        _invalidate_export(project)
        messages.success(request, "Le segment a été enregistré.")
    elif motion_changed:
        _invalidate_video(project)
        messages.success(request, "Le mouvement de la scène a été enregistré.")
    else:
        messages.info(request, "Aucun changement à enregistrer.")
    return redirect(f"/projects/{project.pk}/#segment-{segment.pk}")


@require_POST
def delete_segment(request, project_id, segment_id):
    project = get_object_or_404(NarrationProject, pk=project_id)
    segment = get_object_or_404(NarrationSegment, pk=segment_id, project=project)
    if project.status in {NarrationProject.Status.QUEUED, NarrationProject.Status.PROCESSING}:
        return JsonResponse({"ok": False, "error": "Attends la fin du traitement avant de modifier le projet."}, status=409)
    audio = segment.audio
    image = segment.image
    segment.delete()
    if audio:
        audio.delete(save=False)
    if image:
        image.delete(save=False)
    with transaction.atomic():
        for index, item in enumerate(project.segments.order_by("position", "id")):
            if item.position != index:
                NarrationSegment.objects.filter(pk=item.pk).update(position=index)
    _invalidate_export(project)
    messages.success(request, "Le segment a été supprimé.")
    return redirect("project_detail", project_id=project.pk)


@require_POST
def reorder_segments(request, project_id):
    project = get_object_or_404(NarrationProject, pk=project_id)
    if project.status in {NarrationProject.Status.QUEUED, NarrationProject.Status.PROCESSING}:
        return JsonResponse({"ok": False, "error": "Attends la fin du traitement avant de modifier le projet."}, status=409)
    try:
        ids = json.loads(request.body).get("segment_ids", [])
    except (json.JSONDecodeError, AttributeError):
        return HttpResponseBadRequest("Ordre des segments invalide.")
    actual = set(project.segments.values_list("id", flat=True))
    if set(ids) != actual or len(ids) != len(actual):
        return HttpResponseBadRequest("La liste reçue ne correspond pas aux segments du projet.")
    with transaction.atomic():
        for index, segment_id in enumerate(ids):
            NarrationSegment.objects.filter(project=project, pk=segment_id).update(position=index)
    _invalidate_export(project)
    return JsonResponse({"ok": True})


@require_POST
def delete_project(request, project_id):
    project = get_object_or_404(NarrationProject, pk=project_id)
    if project.status == NarrationProject.Status.PROCESSING:
        messages.error(request, "Attends la fin du traitement avant de supprimer le projet.")
        return redirect("project_detail", project_id=project.pk)
    files = [project.full_audio, project.subtitles, project.video_file]
    files += [file for segment in project.segments.all() for file in (segment.audio, segment.image)]
    project.delete()
    for file in files:
        if file:
            file.delete(save=False)
    messages.success(request, "Le projet a été supprimé.")
    return redirect("home")


@require_POST
def prepare(request, project_id):
    project = get_object_or_404(NarrationProject, pk=project_id)
    if project.status in {NarrationProject.Status.QUEUED, NarrationProject.Status.PROCESSING}:
        return JsonResponse({"ok": False, "error": "Ce projet est déjà dans la file ou en cours de traitement."}, status=409)
    voice_id = request.POST.get("voice_id") or project.voice_id
    voice = get_object_or_404(Voice, pk=voice_id, consent_confirmed=True) if voice_id else None
    segments = list(project.segments.order_by("position", "id"))
    if not voice:
        return JsonResponse({"ok": False, "error": "Choisis une voix autorisée pour ce projet."}, status=400)
    if not segments or any(not segment.text.strip() for segment in segments):
        return JsonResponse({"ok": False, "error": "Ajoute un texte à chaque segment avant de préparer la narration."}, status=400)
    has_images = any(segment.image for segment in segments)
    if has_images and any(not segment.image for segment in segments):
        return JsonResponse({"ok": False, "error": "Ajoute une image à chaque scène avant de générer la vidéo."}, status=400)
    if project.voice_id != voice.pk:
        project.voice = voice
        _invalidate_export(project)
    project.status = NarrationProject.Status.QUEUED
    project.queued_at = timezone.now()
    project.job_type = NarrationProject.JobType.VIDEO if has_images else NarrationProject.JobType.AUDIO
    project.status_message = "Vidéo ajoutée à la file d’attente." if has_images else "Narration ajoutée à la file d’attente."
    project.error_message = ""
    project.progress_percent = 0
    project.current_segment = 0
    project.total_segments = len(segments)
    project.started_at = None
    project.finished_at = None
    project.save()
    return JsonResponse({"ok": True, "status_url": f"/projects/{project.pk}/status/"})


def project_status(request, project_id):
    project = get_object_or_404(NarrationProject, pk=project_id)
    elapsed = 0
    if project.started_at:
        end = project.finished_at or timezone.now()
        elapsed = max(0, round((end - project.started_at).total_seconds()))
    queued = NarrationProject.objects.filter(status=NarrationProject.Status.QUEUED).order_by("queued_at", "id")
    queue_count = queued.count()
    queue_position = next((index for index, item in enumerate(queued.values_list("id", flat=True), start=1)
                           if item == project.pk), 0)
    return JsonResponse({
        "status": project.status,
        "message": project.status_message,
        "error": project.error_message,
        "progress": project.progress_percent,
        "current_segment": project.current_segment,
        "total_segments": project.total_segments,
        "elapsed_seconds": elapsed,
        "queue_position": queue_position,
        "queue_count": queue_count,
        "audio_url": project.full_audio.url if project.full_audio else "",
        "subtitles_url": project.subtitles.url if project.subtitles else "",
        "video_url": project.video_file.url if project.video_file else "",
    })


def segment_subtitles(request, project_id, segment_id):
    project = get_object_or_404(NarrationProject, pk=project_id)
    segment = get_object_or_404(NarrationSegment, pk=segment_id, project=project)
    if not segment.subtitle_data:
        return HttpResponse("Les sous-titres de ce segment ne sont pas prêts.", status=404)
    response = HttpResponse(srt_text([segment]), content_type="application/x-subrip; charset=utf-8")
    response["Content-Disposition"] = f'attachment; filename="segment-{segment.position + 1}.srt"'
    return response
