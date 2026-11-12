import json
from pathlib import Path
import tempfile

from django.contrib import messages
from django.core.files import File
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
MAX_VIDEO_CLIP_BYTES = 200 * 1024 * 1024
MAX_IMPORT_BYTES = 500 * 1024 * 1024
MAX_IMPORT_PIXELS = 100_000_000
ALLOWED_SAMPLE_EXTENSIONS = {".wav", ".mp3", ".webm", ".m4a", ".ogg"}
ALLOWED_VIDEO_EXTENSIONS = {".mp4", ".mov", ".m4v", ".webm"}


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


def _stream_upload_to_path(upload, destination):
    upload.seek(0)
    with destination.open("wb") as output:
        for chunk in upload.chunks():
            output.write(chunk)


def _scene_images(upload, staging_dir, max_scenes=30, remaining_pixels=MAX_IMPORT_PIXELS):
    name = Path(upload.name).name
    suffix = Path(name).suffix.lower()
    if upload.size <= 0:
        raise ValueError(f"Le fichier « {name} » est vide.")
    if upload.size > 25 * 1024 * 1024:
        raise ValueError(f"« {name} » dépasse la limite de 25 Mo.")
    if max_scenes <= 0:
        raise ValueError("Un import est limité à 30 scènes.")
    if suffix not in {".jpg", ".jpeg", ".png", ".pdf"}:
        raise ValueError(f"Format non accepté pour « {name} » : utilise JPG, PNG ou PDF.")

    source_path = staging_dir / f"source{suffix}"
    _stream_upload_to_path(upload, source_path)

    if suffix == ".pdf":
        document = None
        try:
            import pymupdf
            document = pymupdf.open(str(source_path))
            if document.is_encrypted:
                raise ValueError(f"Le PDF « {name} » est protégé par mot de passe.")
            if document.page_count > 30:
                raise ValueError(f"Le PDF « {name} » dépasse la limite de 30 pages par import.")
            if document.page_count > max_scenes:
                raise ValueError("Un import est limité à 30 scènes.")

            output = []
            total_pixels = 0
            for index, page in enumerate(document, start=1):
                pixmap = page.get_pixmap(matrix=pymupdf.Matrix(1.4, 1.4), alpha=False)
                page_pixels = pixmap.width * pixmap.height
                total_pixels += page_pixels
                if page_pixels > 24_000_000 or total_pixels > remaining_pixels:
                    raise ValueError(f"La page {index} du PDF « {name} » dépasse le budget de résolution de l’import.")
                page_path = staging_dir / f"page-{index:03}.png"
                pixmap.save(str(page_path))
                output.append((f"{Path(name).stem}-page-{index:03}.png", page_path))
                del pixmap
            if not output:
                raise ValueError(f"Le PDF « {name} » ne contient aucune page.")
            return output, total_pixels
        except ValueError:
            raise
        except Exception as exc:
            raise ValueError(f"Impossible de lire le PDF « {name} » : {exc}") from exc
        finally:
            if document is not None:
                document.close()

    try:
        with Image.open(source_path) as image:
            image.verify()
        with Image.open(source_path) as image:
            pixels = image.width * image.height
            if pixels > 40_000_000:
                raise ValueError(f"L’image « {name} » a une résolution trop élevée.")
            if pixels > remaining_pixels:
                raise ValueError(f"L’image « {name} » dépasse le budget de résolution de l’import.")
    except ValueError:
        raise
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as exc:
        raise ValueError(f"Impossible de lire l’image « {name} » : {exc}") from exc
    return [(name, source_path)], pixels


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
    return render(request, "studio/project_editor.html", {
        "project": project,
        "projects": NarrationProject.objects.all()[:30],
        "segments": segments,
        "voices": Voice.objects.all(),
        "categories": Voice.Category.choices,
        "video_formats": NarrationProject.VideoFormat.choices,
        "motions": NarrationSegment.Motion.choices,
        "has_scenes": bool(segments and any(segment.image for segment in segments)),
        "has_video_clips": bool(segments and any(segment.video_clip for segment in segments)),
        "can_assemble_clips": bool(
            segments and all(segment.video_clip and not segment.image for segment in segments)
        ),
    })


@require_POST
def import_scenes(request, project_id):
    project = get_object_or_404(NarrationProject, pk=project_id)
    if project.status in {NarrationProject.Status.QUEUED, NarrationProject.Status.PROCESSING}:
        return JsonResponse({"ok": False, "error": "Attends la fin du traitement avant d’ajouter des scènes."}, status=409)
    uploads = request.FILES.getlist("files")
    if not uploads:
        messages.error(request, "Sélectionne au moins une image, un PDF ou une vidéo.")
        return redirect("project_detail", project_id=project.pk)

    total_upload_bytes = sum(upload.size for upload in uploads)
    if total_upload_bytes > MAX_IMPORT_BYTES:
        messages.error(request, "Un import ne peut pas dépasser 500 Mo au total.")
        return redirect("project_detail", project_id=project.pk)

    try:
        with tempfile.TemporaryDirectory(prefix="petit-studio-import-") as temporary_dir:
            staging_root = Path(temporary_dir)
            scenes = []
            import_kind = None
            total_pixels = 0

            for upload_index, upload in enumerate(uploads, start=1):
                suffix = Path(upload.name).suffix.lower()
                if suffix in ALLOWED_VIDEO_EXTENSIONS:
                    kind = "video"
                elif suffix in {".jpg", ".jpeg", ".png", ".pdf"}:
                    kind = "image"
                else:
                    raise ValueError(
                        f"Format non accepté pour « {Path(upload.name).name} » : "
                        "utilise JPG, PNG, PDF ou une vidéo MP4/MOV/WEBM."
                    )

                if import_kind and kind != import_kind:
                    raise ValueError("Importe soit des vidéos, soit des images/PDF dans un même projet, sans les mélanger.")
                import_kind = kind

                upload_dir = staging_root / f"upload-{upload_index:03}"
                upload_dir.mkdir()

                if kind == "video":
                    if upload.size <= 0:
                        raise ValueError(f"La vidéo « {Path(upload.name).name} » est vide.")
                    if upload.size > MAX_VIDEO_CLIP_BYTES:
                        raise ValueError(f"La vidéo « {Path(upload.name).name} » dépasse la limite de 200 Mo.")
                    if len(scenes) >= 30:
                        raise ValueError("Un import est limité à 30 scènes.")
                    staged_path = upload_dir / f"source{suffix}"
                    _stream_upload_to_path(upload, staged_path)
                    scenes.append((kind, Path(upload.name).name, staged_path))
                else:
                    staged_scenes, imported_pixels = _scene_images(
                        upload,
                        upload_dir,
                        max_scenes=30 - len(scenes),
                        remaining_pixels=MAX_IMPORT_PIXELS - total_pixels,
                    )
                    scenes.extend((kind, filename, path) for filename, path in staged_scenes)
                    total_pixels += imported_pixels

            existing = list(project.segments.order_by("position", "id"))
            existing_kind = (
                "video" if any(item.video_clip for item in existing)
                else "image" if any(item.image for item in existing)
                else None
            )
            if existing_kind and existing_kind != import_kind:
                messages.error(
                    request,
                    "Ce projet contient déjà un autre type de média. "
                    "Crée un projet séparé pour assembler tes vidéos.",
                )
                return redirect("project_detail", project_id=project.pk)

            reusable = next(
                (item for item in existing if not item.image and not item.video_clip and not item.text.strip()),
                None,
            )
            if reusable is None:
                reusable = next((item for item in existing if not item.image and not item.video_clip), None)

            for kind, filename, staged_path in scenes:
                segment = reusable
                if reusable:
                    reusable = None
                else:
                    segment = NarrationSegment.objects.create(
                        project=project,
                        position=project.segments.count(),
                    )

                if kind == "video":
                    if segment.image:
                        segment.image.delete(save=False)
                        segment.image = ""
                    if segment.video_clip:
                        segment.video_clip.delete(save=False)
                    with staged_path.open("rb") as staged_file:
                        segment.video_clip.save(filename, File(staged_file), save=False)
                    segment.save(update_fields=["image", "video_clip", "updated_at"])
                else:
                    if segment.video_clip:
                        segment.video_clip.delete(save=False)
                        segment.video_clip = ""
                    if segment.image:
                        segment.image.delete(save=False)
                    with staged_path.open("rb") as staged_file:
                        segment.image.save(filename, File(staged_file), save=False)
                    segment.save(update_fields=["image", "video_clip", "updated_at"])

            _invalidate_export(project)
            if import_kind == "video":
                messages.success(
                    request,
                    f"{len(scenes)} clip(s) vidéo ajouté(s). Réordonne-les puis lance l’assemblage.",
                )
            else:
                messages.success(request, f"{len(scenes)} scène(s) ajoutée(s) au projet.")
    except ValueError as exc:
        messages.error(request, str(exc))
    return redirect("project_detail", project_id=project.pk)


@require_POST
def assemble_videos(request, project_id):
    project = get_object_or_404(NarrationProject, pk=project_id)
    if project.status in {NarrationProject.Status.QUEUED, NarrationProject.Status.PROCESSING}:
        return JsonResponse({"ok": False, "error": "Ce projet est déjà dans la file ou en cours de traitement."}, status=409)
    segments = list(project.segments.order_by("position", "id"))
    if not segments or any(not item.video_clip or item.image for item in segments):
        return JsonResponse({"ok": False, "error": "Ajoute uniquement des clips vidéo à ce projet avant de les assembler."}, status=400)
    project.status = NarrationProject.Status.QUEUED
    project.queued_at = timezone.now()
    project.job_type = NarrationProject.JobType.ASSEMBLY
    project.status_message = "Assemblage vidéo ajouté à la file d’attente."
    project.error_message = ""
    project.progress_percent = 0
    project.current_segment = 0
    project.total_segments = len(segments)
    project.started_at = None
    project.finished_at = None
    project.save(update_fields=[
        "status", "queued_at", "job_type", "status_message", "error_message", "progress_percent",
        "current_segment", "total_segments", "started_at", "finished_at", "updated_at",
    ])
    return JsonResponse({"ok": True, "status_url": f"/projects/{project.pk}/status/"})


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
    video_clip = segment.video_clip
    segment.delete()
    if audio:
        audio.delete(save=False)
    if image:
        image.delete(save=False)
    if video_clip:
        video_clip.delete(save=False)
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
    files += [file for segment in project.segments.all() for file in (segment.audio, segment.image, segment.video_clip)]
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
    has_video_clips = any(segment.video_clip for segment in segments)
    if has_video_clips:
        return JsonResponse({"ok": False, "error": "Pour assembler ces clips avec leur son d’origine, utilise le bouton « Assembler les clips vidéo »."}, status=400)
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
