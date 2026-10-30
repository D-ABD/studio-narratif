import json
from pathlib import Path
import threading

from django.contrib import messages
from django.core.files.base import ContentFile
from django.db import close_old_connections, transaction
from django.http import HttpResponse, HttpResponseBadRequest, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from .models import NarrationProject, NarrationSegment, Voice
from .services import cache_key_for, hash_file, normalize_voice_sample, prepare_project, srt_text

MAX_SAMPLE_BYTES = 25 * 1024 * 1024
ALLOWED_SAMPLE_EXTENSIONS = {".wav", ".mp3", ".webm", ".m4a", ".ogg"}


def _invalidate_export(project):
    if project.full_audio:
        project.full_audio.delete(save=False)
    if project.subtitles:
        project.subtitles.delete(save=False)
    project.full_audio = ""
    project.subtitles = ""
    project.duration_seconds = 0
    project.status = NarrationProject.Status.DRAFT
    project.status_message = "Modifications en attente de préparation."
    project.error_message = ""
    project.progress_percent = 0
    project.started_at = None
    project.finished_at = None
    project.save(update_fields=[
        "voice", "full_audio", "subtitles", "duration_seconds", "status", "status_message", "error_message",
        "progress_percent", "started_at", "finished_at", "updated_at",
    ])


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
    for segment in segments:
        try:
            segment.ready_for_voice = bool(
                project.voice_id and segment.text.strip()
                and segment.cache_key == cache_key_for(segment.text, project.voice)
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
    })


@require_POST
def set_project_voice(request, project_id):
    project = get_object_or_404(NarrationProject, pk=project_id)
    voice_id = request.POST.get("voice_id")
    voice = get_object_or_404(Voice, pk=voice_id, consent_confirmed=True) if voice_id else None
    if project.status == NarrationProject.Status.PROCESSING:
        return JsonResponse({"ok": False, "error": "Un traitement est déjà en cours."}, status=409)
    if project.voice_id != (voice.pk if voice else None):
        project.voice = voice
        _invalidate_export(project)
    return redirect("project_detail", project_id=project.pk)


@require_POST
def add_segment(request, project_id):
    project = get_object_or_404(NarrationProject, pk=project_id)
    if project.status == NarrationProject.Status.PROCESSING:
        return JsonResponse({"ok": False, "error": "Attends la fin du traitement avant de modifier le projet."}, status=409)
    NarrationSegment.objects.create(project=project, position=project.segments.count())
    _invalidate_export(project)
    return redirect("project_detail", project_id=project.pk)


@require_POST
def save_segment(request, project_id, segment_id):
    project = get_object_or_404(NarrationProject, pk=project_id)
    segment = get_object_or_404(NarrationSegment, pk=segment_id, project=project)
    if project.status == NarrationProject.Status.PROCESSING:
        return JsonResponse({"ok": False, "error": "Attends la fin du traitement avant de modifier le projet."}, status=409)
    text = (request.POST.get("text") or "").strip()[:12000]
    if text != segment.text:
        if segment.audio:
            segment.audio.delete(save=False)
        segment.text = text
        segment.audio = ""
        segment.subtitle_data = []
        segment.duration_seconds = 0
        segment.cache_key = ""
        segment.save(update_fields=["text", "audio", "subtitle_data", "duration_seconds", "cache_key", "updated_at"])
        _invalidate_export(project)
        messages.success(request, "Le segment a été enregistré.")
    else:
        messages.info(request, "Aucun changement à enregistrer.")
    return redirect(f"/projects/{project.pk}/#segment-{segment.pk}")


@require_POST
def delete_segment(request, project_id, segment_id):
    project = get_object_or_404(NarrationProject, pk=project_id)
    segment = get_object_or_404(NarrationSegment, pk=segment_id, project=project)
    if project.status == NarrationProject.Status.PROCESSING:
        return JsonResponse({"ok": False, "error": "Attends la fin du traitement avant de modifier le projet."}, status=409)
    audio = segment.audio
    segment.delete()
    if audio:
        audio.delete(save=False)
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
    if project.status == NarrationProject.Status.PROCESSING:
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
    files = [project.full_audio, project.subtitles]
    files += [segment.audio for segment in project.segments.all()]
    project.delete()
    for file in files:
        if file:
            file.delete(save=False)
    messages.success(request, "Le projet a été supprimé.")
    return redirect("home")


@require_POST
def prepare(request, project_id):
    project = get_object_or_404(NarrationProject, pk=project_id)
    if project.status == NarrationProject.Status.PROCESSING:
        return JsonResponse({"ok": False, "error": "Un traitement est déjà en cours."}, status=409)
    voice_id = request.POST.get("voice_id") or project.voice_id
    voice = get_object_or_404(Voice, pk=voice_id, consent_confirmed=True) if voice_id else None
    segments = list(project.segments.order_by("position", "id"))
    if not voice:
        return JsonResponse({"ok": False, "error": "Choisis une voix autorisée pour ce projet."}, status=400)
    if not segments or any(not segment.text.strip() for segment in segments):
        return JsonResponse({"ok": False, "error": "Ajoute un texte à chaque segment avant de préparer la narration."}, status=400)
    if project.voice_id != voice.pk:
        project.voice = voice
        _invalidate_export(project)
    project.status = NarrationProject.Status.PROCESSING
    project.status_message = "Mise en attente du moteur vocal…"
    project.error_message = ""
    project.progress_percent = 0
    project.current_segment = 0
    project.total_segments = len(segments)
    project.started_at = timezone.now()
    project.finished_at = None
    project.save()
    worker = threading.Thread(target=_run_prepare, args=(project.pk,), daemon=True, name=f"narration-{project.pk}")
    worker.start()
    return JsonResponse({"ok": True, "status_url": f"/projects/{project.pk}/status/"})


def _run_prepare(project_id):
    close_old_connections()
    try:
        prepare_project(project_id)
    finally:
        close_old_connections()


def project_status(request, project_id):
    project = get_object_or_404(NarrationProject, pk=project_id)
    elapsed = 0
    if project.started_at:
        end = project.finished_at or timezone.now()
        elapsed = max(0, round((end - project.started_at).total_seconds()))
    return JsonResponse({
        "status": project.status,
        "message": project.status_message,
        "error": project.error_message,
        "progress": project.progress_percent,
        "current_segment": project.current_segment,
        "total_segments": project.total_segments,
        "elapsed_seconds": elapsed,
        "audio_url": project.full_audio.url if project.full_audio else "",
        "subtitles_url": project.subtitles.url if project.subtitles else "",
    })


def segment_subtitles(request, project_id, segment_id):
    project = get_object_or_404(NarrationProject, pk=project_id)
    segment = get_object_or_404(NarrationSegment, pk=segment_id, project=project)
    if not segment.subtitle_data:
        return HttpResponse("Les sous-titres de ce segment ne sont pas prêts.", status=404)
    response = HttpResponse(srt_text([segment]), content_type="application/x-subrip; charset=utf-8")
    response["Content-Disposition"] = f'attachment; filename="segment-{segment.position + 1}.srt"'
    return response
