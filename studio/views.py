import json
import logging
import subprocess
from functools import wraps
from pathlib import Path
import tempfile
import uuid

from django.contrib import messages
from django.contrib.auth import login
from django.contrib.auth.decorators import login_required
from django.contrib.auth.forms import UserCreationForm
from django.core.files import File
from django.core.files.base import ContentFile
from django.db import transaction
from django.http import HttpResponse, HttpResponseBadRequest, JsonResponse
from django.urls import reverse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_GET, require_http_methods, require_POST
from PIL import Image, UnidentifiedImageError

from .models import (
    ANIMATION_EFFECTS, ANIMATION_LEVEL_CHOICES, AudioAsset, BackgroundTrack, NarrationProject,
    NarrationSegment, Voice,
    default_animation_settings, legacy_animation_settings,
)
from .services import _voice_sample_digest, cache_key_for, hash_file, normalize_voice_sample, srt_text

logger = logging.getLogger(__name__)


def json_login_required(view_func):
    @wraps(view_func)
    def wrapped(request, *args, **kwargs):
        if not request.user.is_authenticated:
            return JsonResponse({"error": "auth_required"}, status=401)
        return view_func(request, *args, **kwargs)
    return wrapped

MAX_SAMPLE_BYTES = 25 * 1024 * 1024
MAX_VIDEO_CLIP_BYTES = 200 * 1024 * 1024
MAX_IMPORT_BYTES = 500 * 1024 * 1024
MAX_IMPORT_PIXELS = 100_000_000
MAX_MUSIC_BYTES = 50 * 1024 * 1024
MAX_AUDIO_ASSET_BYTES = 100 * 1024 * 1024
ALLOWED_MUSIC_EXTENSIONS = {".mp3", ".wav", ".m4a", ".ogg"}
ALLOWED_AUDIO_ASSET_EXTENSIONS = {".mp3", ".wav", ".m4a", ".ogg"}
ALLOWED_SAMPLE_EXTENSIONS = {".wav", ".mp3", ".webm", ".m4a", ".ogg"}
ALLOWED_VIDEO_EXTENSIONS = {".mp4", ".mov", ".m4v", ".webm"}
ANIMATION_EFFECT_DEFINITIONS = (
    {"key": "zoom", "label": "Zoom", "directions": (("in", "Avant"), ("out", "Arrière"))},
    {"key": "pan", "label": "Panoramique", "directions": (
        ("horizontal", "Horizontal"), ("vertical", "Vertical"), ("diagonal", "Diagonal"),
    )},
    {"key": "parallax", "label": "Parallaxe 2.5D", "directions": ()},
    {"key": "handheld", "label": "Caméra à main levée", "directions": ()},
    {"key": "light_breathing", "label": "Respiration lumineuse", "directions": ()},
    {"key": "light_leak", "label": "Fuite lumineuse", "directions": ()},
)


def _validate_animation_settings_json(raw_value):
    if not isinstance(raw_value, str) or len(raw_value) > 8000:
        raise ValueError("Réglages d’animation absents ou trop volumineux.")
    try:
        settings = json.loads(raw_value)
    except (json.JSONDecodeError, TypeError) as exc:
        raise ValueError("Le JSON des réglages d’animation est invalide.") from exc
    if not isinstance(settings, dict) or set(settings) != {"version", "camera_profile", "effects"}:
        raise ValueError("La structure des réglages d’animation est invalide.")
    if type(settings["version"]) is not int or settings["version"] != 1:
        raise ValueError("La version des réglages d’animation n’est pas prise en charge.")
    if not isinstance(settings["camera_profile"], str) or settings["camera_profile"] not in NarrationSegment.CameraProfile.values:
        raise ValueError("Profil de caméra invalide.")
    effects = settings["effects"]
    if not isinstance(effects, dict) or set(effects) != set(ANIMATION_EFFECTS):
        raise ValueError("La liste des effets d’animation est invalide.")

    levels = {value for value, _label in ANIMATION_LEVEL_CHOICES}
    expected_fields = {
        "zoom": {"enabled", "level", "direction"},
        "pan": {"enabled", "level", "direction"},
        "parallax": {"enabled", "level"},
        "handheld": {"enabled", "level"},
        "light_breathing": {"enabled", "level"},
        "light_leak": {"enabled", "level"},
    }
    allowed_directions = {
        "zoom": {"in", "out"},
        "pan": {"horizontal", "vertical", "diagonal"},
    }
    for key, required_fields in expected_fields.items():
        config = effects[key]
        if not isinstance(config, dict) or set(config) != required_fields:
            raise ValueError(f"Réglages invalides pour l’effet {key}.")
        if (type(config["enabled"]) is not bool or not isinstance(config["level"], str)
                or config["level"] not in levels):
            raise ValueError(f"Activation ou niveau invalide pour l’effet {key}.")
        if key in allowed_directions and (
            not isinstance(config["direction"], str)
            or config["direction"] not in allowed_directions[key]
        ):
            raise ValueError(f"Direction invalide pour l’effet {key}.")
    return settings


def _legacy_motion_from_animation(settings):
    effects = settings["effects"]
    if effects["parallax"]["enabled"]:
        return NarrationSegment.Motion.PARALLAX
    zoom = effects["zoom"]
    pan = effects["pan"]
    if zoom["enabled"] and pan["enabled"]:
        return NarrationSegment.Motion.ZOOM_PAN
    if pan["enabled"]:
        return (
            NarrationSegment.Motion.PAN_V
            if pan["direction"] == "vertical"
            else NarrationSegment.Motion.PAN_H
        )
    if zoom["enabled"]:
        return (
            NarrationSegment.Motion.ZOOM_OUT
            if zoom["direction"] == "out"
            else NarrationSegment.Motion.ZOOM_IN
        )
    return NarrationSegment.Motion.STILL


def _settings_for_legacy_post(request, segment):
    motion = request.POST.get("motion", segment.motion)
    camera_profile = request.POST.get("camera_profile", segment.camera_profile)
    if motion not in NarrationSegment.Motion.values:
        raise ValueError("Mouvement de scène invalide.")
    if camera_profile not in NarrationSegment.CameraProfile.values:
        raise ValueError("Profil de caméra invalide.")
    return legacy_animation_settings(
        motion,
        camera_profile,
        request.POST.get("light_breathing") == "on",
    )


def _delete_storage_files(files):
    for storage, name in files:
        if not name:
            continue
        try:
            storage.delete(name)
        except Exception:
            logger.exception("Impossible de supprimer le fichier média %s.", name)


def _schedule_storage_cleanup(files):
    files_to_delete = tuple(files)
    if files_to_delete:
        transaction.on_commit(lambda: _delete_storage_files(files_to_delete))


def _save_staged_file(field_file, filename, staged_path, created_files):
    source_name = Path(filename)
    unique_name = f"{source_name.stem}-{uuid.uuid4().hex}{source_name.suffix}"
    storage = field_file.storage
    expected_name = field_file.field.generate_filename(field_file.instance, unique_name)
    # Track the intended path before writing so a partial storage write can be cleaned too.
    created_files.append((storage, expected_name))
    with staged_path.open("rb") as staged_file:
        field_file.save(unique_name, File(staged_file), save=False)
    if field_file.name != expected_name:
        created_files.append((storage, field_file.name))


def _invalidate_export(project):
    files_to_delete = [
        (field.storage, field.name)
        for field in (project.full_audio, project.subtitles, project.video_file)
        if field and field.name
    ]
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
    with transaction.atomic():
        project.save(update_fields=[
            "voice", "full_audio", "subtitles", "video_file", "duration_seconds", "status", "status_message", "error_message",
            "progress_percent", "queued_at", "job_type", "started_at", "finished_at", "updated_at",
        ])
        _schedule_storage_cleanup(files_to_delete)


def _invalidate_video(project, extra_update_fields=()):
    update_fields = ["updated_at"]
    files_to_delete = []
    if project.video_file:
        files_to_delete.append((project.video_file.storage, project.video_file.name))
        project.video_file = ""
        update_fields.append("video_file")
    if project.status == NarrationProject.Status.READY:
        project.status_message = "Les réglages vidéo ont changé. Génère une nouvelle vidéo."
        update_fields.append("status_message")
    update_fields.extend(extra_update_fields)
    project.save(update_fields=update_fields)
    _schedule_storage_cleanup(files_to_delete)


def _probe_music_duration(path):
    """Validate that ffprobe can read an audio stream and return its duration."""
    result = subprocess.run(
        [
            "ffprobe", "-v", "error", "-show_entries", "format=duration",
            "-show_entries", "stream=codec_type,duration", "-of", "json", str(path),
        ],
        capture_output=True, text=True, check=False, timeout=60,
    )
    if result.returncode != 0:
        raise ValueError("FFprobe ne reconnaît pas ce fichier audio.")
    try:
        metadata = json.loads(result.stdout)
        if not any(stream.get("codec_type") == "audio" for stream in metadata.get("streams", [])):
            raise ValueError("Le fichier ne contient aucune piste audio.")
        duration = float(metadata.get("format", {}).get("duration") or 0)
        if duration <= 0:
            durations = [
                float(stream.get("duration") or 0)
                for stream in metadata.get("streams", [])
                if stream.get("codec_type") == "audio"
            ]
            duration = max(durations, default=0)
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        if isinstance(exc, ValueError) and str(exc).startswith("Le fichier"):
            raise
        raise ValueError("Impossible de détecter la durée de cette musique.") from exc
    if duration <= 0:
        raise ValueError("Impossible de détecter la durée de cette musique.")
    return duration


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


@require_http_methods(["GET", "POST"])
def register(request):
    if request.user.is_authenticated:
        return redirect("home")
    if request.method == "POST":
        form = UserCreationForm(request.POST)
        if form.is_valid():
            user = form.save()
            login(request, user)
            return redirect("home")
    else:
        form = UserCreationForm()
    return render(request, "registration/register.html", {"form": form})


@login_required
def home(request):
    query = (request.GET.get("q") or "").strip()[:80]
    category = request.GET.get("category", "")
    voices = Voice.objects.filter(user=request.user)
    if query:
        voices = voices.filter(name__icontains=query)
    if category in Voice.Category.values:
        voices = voices.filter(category=category)
    return render(request, "studio/home.html", {
        "voices": voices,
        "projects": NarrationProject.objects.filter(user=request.user)[:30],
        "voice_count": Voice.objects.filter(user=request.user).count(),
        "project_count": NarrationProject.objects.filter(user=request.user).count(),
        "query": query,
        "selected_category": category,
        "categories": Voice.Category.choices,
    })


@login_required
def musics(request):
    return render(request, "studio/musics.html", {
        "background_tracks": BackgroundTrack.objects.filter(user=request.user),
        "projects": NarrationProject.objects.filter(user=request.user)[:30],
    })


@login_required
def audios(request):
    return _audio_library_response(request)


def _audio_library_response(request, *, status=200):
    audio_assets = list(
        AudioAsset.objects.filter(user=request.user).order_by("-created_at", "-id")
    )
    for asset in audio_assets:
        minutes, seconds = divmod(max(0, round(asset.duration)), 60)
        asset.duration_mmss = f"{minutes:02d}:{seconds:02d}"
    return render(request, "studio/audios.html", {
        "audio_assets": audio_assets,
        "projects": NarrationProject.objects.filter(user=request.user)[:30],
    }, status=status)


def _unlink_audio_asset_file(path):
    try:
        Path(path).unlink()
    except FileNotFoundError:
        logger.warning("Le fichier audio %s était déjà absent lors de sa suppression.", path)
    except OSError:
        logger.warning("Impossible de supprimer le fichier audio %s après suppression en base.", path, exc_info=True)


@login_required
@require_POST
def upload_audio_asset(request):
    upload = request.FILES.get("audio_file")
    if not upload or upload.size <= 0:
        messages.error(request, "Sélectionne un fichier audio.")
        return _audio_library_response(request, status=400)
    if upload.size > MAX_AUDIO_ASSET_BYTES:
        messages.error(request, "Le fichier audio dépasse la limite de 100 Mo.")
        return _audio_library_response(request, status=400)
    extension = Path(upload.name).suffix.lower()
    if extension not in ALLOWED_AUDIO_ASSET_EXTENSIONS:
        messages.error(request, "Formats acceptés : MP3, WAV, M4A et OGG.")
        return _audio_library_response(request, status=400)

    asset = None
    created_file = None
    try:
        with tempfile.TemporaryDirectory(prefix="petit-studio-audio-asset-") as folder:
            staged_path = Path(folder) / f"upload{extension}"
            _stream_upload_to_path(upload, staged_path)
            duration = _probe_music_duration(staged_path)
            name = Path(upload.name).stem.strip()[:100] or "Audio importé"
            asset = AudioAsset(
                user=request.user,
                name=name,
                duration=duration,
                source=AudioAsset.Source.UPLOAD,
            )
            with staged_path.open("rb") as stream:
                asset.audio_file.save(Path(upload.name).name, File(stream), save=False)
            if asset.audio_file.name:
                created_file = (asset.audio_file.storage, asset.audio_file.name)
            asset.save()
        messages.success(request, f"L’audio « {asset.name} » a été ajouté à ta bibliothèque.")
        return redirect("audios")
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        if created_file:
            _delete_storage_files([created_file])
        logger.info("Import audio refusé pour l’utilisateur %s : %s", request.user.pk, exc)
        messages.error(request, str(exc) or "Impossible de lire ce fichier audio.")
        return _audio_library_response(request, status=400)
    except Exception:
        if created_file:
            _delete_storage_files([created_file])
        logger.exception("Échec de l’enregistrement d’un audio pour l’utilisateur %s.", request.user.pk)
        messages.error(request, "Impossible d’enregistrer cet audio.")
        return _audio_library_response(request, status=500)


@login_required
@require_POST
def rename_audio_asset(request, asset_id):
    asset = get_object_or_404(AudioAsset, pk=asset_id, user=request.user)
    name = (request.POST.get("name") or "").strip()
    if not name:
        messages.error(request, "Le nom de l’audio ne peut pas être vide.")
        return _audio_library_response(request, status=400)
    if len(name) > 100:
        messages.error(request, "Le nom de l’audio ne peut pas dépasser 100 caractères.")
        return _audio_library_response(request, status=400)

    asset.name = name
    asset.save(update_fields=["name", "updated_at"])
    messages.success(request, f"L’audio a été renommé « {asset.name} ».")
    return redirect("audios")


@login_required
@require_POST
def delete_audio_asset(request, asset_id):
    if request.POST.get("confirm") != "1":
        messages.error(request, "Confirmation manquante")
        return _audio_library_response(request, status=400)

    with transaction.atomic():
        asset = get_object_or_404(
            AudioAsset.objects.select_for_update(), pk=asset_id, user=request.user,
        )
        if asset.project_id is not None:
            messages.error(request, "Impossible de supprimer cet audio : il est référencé par un projet.")
            return _audio_library_response(request, status=400)
        audio_path = asset.audio_file.path if asset.audio_file else None
        asset.delete()
        if audio_path:
            transaction.on_commit(lambda path=audio_path: _unlink_audio_asset_file(path))

    messages.success(request, f"L’audio « {asset.name} » a été supprimé.")
    return redirect("audios")


@login_required
@require_POST
def upload_music(request):
    upload = request.FILES.get("audio_file")
    if not upload or upload.size <= 0:
        messages.error(request, "Sélectionne un fichier musical.")
        return redirect("musics")
    if upload.size > MAX_MUSIC_BYTES:
        messages.error(request, "La musique dépasse la limite de 50 Mo.")
        return redirect("musics")
    extension = Path(upload.name).suffix.lower()
    if extension not in ALLOWED_MUSIC_EXTENSIONS:
        messages.error(request, "Formats acceptés : MP3, WAV, M4A et OGG.")
        return redirect("musics")

    title = (request.POST.get("name") or Path(upload.name).stem).strip()[:120]
    if not title:
        title = "Musique sans titre"
    try:
        with tempfile.TemporaryDirectory(prefix="petit-studio-music-") as folder:
            staged_path = Path(folder) / f"upload{extension}"
            _stream_upload_to_path(upload, staged_path)
            duration = _probe_music_duration(staged_path)
            track = BackgroundTrack(user=request.user, name=title, duration=duration)
            with staged_path.open("rb") as stream:
                track.audio_file.save(upload.name, File(stream), save=False)
            try:
                track.save()
            except Exception:
                if track.audio_file.name:
                    track.audio_file.storage.delete(track.audio_file.name)
                raise
        messages.success(request, f"La musique « {track.name} » a été ajoutée à votre bibliothèque.")
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        messages.error(request, str(exc) or "Impossible d’ajouter cette musique.")
    except Exception:
        logger.exception("Échec de l’enregistrement d’une musique pour l’utilisateur %s.", request.user.pk)
        messages.error(request, "Impossible d’enregistrer cette musique.")
    return redirect("musics")


@login_required
@require_POST
def delete_music(request, track_id):
    track = get_object_or_404(BackgroundTrack, pk=track_id, user=request.user)
    if request.POST.get("confirm") != "yes":
        messages.error(request, "La suppression n’a pas été confirmée.")
        return redirect("musics")

    projects = list(NarrationProject.objects.filter(user=request.user, background_track=track).order_by("pk"))
    if any(project.status in {NarrationProject.Status.QUEUED, NarrationProject.Status.PROCESSING} for project in projects):
        messages.error(request, "Cette musique est utilisée par un projet en attente ou en cours de traitement.")
        return redirect("musics")

    files_to_delete = [(track.audio_file.storage, track.audio_file.name)] if track.audio_file else []
    with transaction.atomic():
        for project in projects:
            if project.video_file:
                files_to_delete.append((project.video_file.storage, project.video_file.name))
            project.background_track = None
            project.video_file = ""
            fields = ["background_track", "video_file", "updated_at"]
            if project.status == NarrationProject.Status.READY:
                project.status_message = "La musique associée a été supprimée. Génère une nouvelle vidéo."
                fields.append("status_message")
            project.save(update_fields=fields)
        track.delete()
        _schedule_storage_cleanup(files_to_delete)
    messages.success(request, f"La musique « {track.name} » a été supprimée.")
    return redirect("musics")


@login_required
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
        voice = Voice(user=request.user, name=name, category=category, consent_confirmed=True)
        voice.sample.save(f"voice-{timezone.now():%Y%m%d-%H%M%S}.wav", ContentFile(wav_bytes), save=False)
        voice.sample_sha256 = hash_file(voice.sample.path) if voice.sample.name else ""
        voice.save()
        messages.success(request, f"La voix « {voice.name} » est enregistrée sur cet appareil.")
    except Exception as exc:
        messages.error(request, str(exc) or "Impossible d’enregistrer cet échantillon audio.")
    return redirect("home")


@login_required
@require_POST
def edit_voice(request, voice_id):
    voice = get_object_or_404(Voice, pk=voice_id, user=request.user)
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


@login_required
@require_POST
def delete_voice(request, voice_id):
    voice = get_object_or_404(Voice, pk=voice_id, user=request.user)
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



def _queue_queryset(user):
    return NarrationProject.objects.filter(
        user=user,
        status__in=[NarrationProject.Status.PROCESSING, NarrationProject.Status.QUEUED],
    ).order_by("queued_at", "id")


@login_required
def queue_view(request):
    active_projects = list(_queue_queryset(request.user))
    processing_projects = [project for project in active_projects if project.status == NarrationProject.Status.PROCESSING]
    waiting_projects = [project for project in active_projects if project.status == NarrationProject.Status.QUEUED]
    return render(request, "studio/queue.html", {
        "processing_projects": processing_projects,
        "waiting_projects": waiting_projects,
        "projects": NarrationProject.objects.filter(user=request.user)[:30],
    })


def _queue_project_payload(project, position=None):
    return {
        "id": project.pk,
        "title": project.title,
        "status": project.status,
        "status_label": project.get_status_display(),
        "progress": project.progress_percent,
        "current_segment": project.current_segment,
        "total_segments": project.total_segments,
        "cancel_requested": project.cancel_requested,
        "position": position,
        "detail_url": reverse("project_detail", args=[project.pk]),
        "cancel_url": reverse("cancel_job", args=[project.pk]),
    }


@require_GET
@login_required
def queue_status(request):
    projects = list(_queue_queryset(request.user))
    processing = [
        _queue_project_payload(project)
        for project in projects
        if project.status == NarrationProject.Status.PROCESSING
    ]
    waiting = [
        _queue_project_payload(project, position=index)
        for index, project in enumerate(
            (project for project in projects if project.status == NarrationProject.Status.QUEUED),
            start=1,
        )
    ]
    return JsonResponse({
        "processing": processing,
        "waiting": waiting,
        "queue_count": len(waiting),
        "is_empty": not processing and not waiting,
    })


@login_required
@require_POST
def create_project(request):
    title = (request.POST.get("title") or "").strip()[:120]
    if not title:
        title = timezone.localtime().strftime("Projet du %d/%m/%Y à %Hh%M")
    project = NarrationProject.objects.create(user=request.user, title=title)
    NarrationSegment.objects.create(project=project, position=0)
    return redirect("project_detail", project_id=project.pk)


@login_required
def project_detail(request, project_id):
    project = get_object_or_404(NarrationProject, pk=project_id, user=request.user)
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
        animation = segment.animation_settings or default_animation_settings()
        segment.animation_settings_json = json.dumps(animation, ensure_ascii=False, separators=(",", ":"))
        segment.animation_controls = []
        for definition in ANIMATION_EFFECT_DEFINITIONS:
            config = animation["effects"][definition["key"]]
            segment.animation_controls.append({
                **definition,
                "enabled": config["enabled"],
                "level": config["level"],
                "direction": config.get("direction", ""),
            })
    return render(request, "studio/project_editor.html", {
        "project": project,
        "projects": NarrationProject.objects.filter(user=request.user)[:30],
        "segments": segments,
        "audio_ready": bool(segments and all(segment.ready_for_voice for segment in segments)),
        "voices": Voice.objects.filter(user=request.user),
        "background_tracks": BackgroundTrack.objects.filter(user=request.user),
        "categories": Voice.Category.choices,
        "video_formats": NarrationProject.VideoFormat.choices,
        "motions": NarrationSegment.Motion.choices,
        "camera_profiles": NarrationSegment.CameraProfile.choices,
        "animation_levels": ANIMATION_LEVEL_CHOICES,
        "has_scenes": bool(segments and any(segment.image for segment in segments)),
        "has_video_clips": bool(segments and any(segment.video_clip for segment in segments)),
        "can_assemble_clips": bool(
            segments and all(segment.video_clip and not segment.image for segment in segments)
        ),
        "can_narrate_video": bool(
            segments and all(
                segment.video_clip and not segment.image and segment.text.strip()
                for segment in segments
            )
        ),
    })


@login_required
@require_POST
def import_scenes(request, project_id):
    project = get_object_or_404(NarrationProject, pk=project_id, user=request.user)
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

    created_files = []
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

            old_files = []
            planned_segments = []
            next_position = len(existing)
            for kind, filename, staged_path in scenes:
                if reusable:
                    segment = reusable
                    reusable = None
                    is_new = False
                else:
                    segment = NarrationSegment(project=project, position=next_position)
                    next_position += 1
                    is_new = True

                if kind == "video":
                    if segment.image and segment.image.name:
                        old_files.append((segment.image.storage, segment.image.name))
                    if segment.video_clip and segment.video_clip.name:
                        old_files.append((segment.video_clip.storage, segment.video_clip.name))
                    segment.image = ""
                    _save_staged_file(segment.video_clip, filename, staged_path, created_files)
                else:
                    if segment.video_clip and segment.video_clip.name:
                        old_files.append((segment.video_clip.storage, segment.video_clip.name))
                    if segment.image and segment.image.name:
                        old_files.append((segment.image.storage, segment.image.name))
                    segment.video_clip = ""
                    _save_staged_file(segment.image, filename, staged_path, created_files)

                planned_segments.append((segment, is_new, ["image", "video_clip", "updated_at"]))

            with transaction.atomic():
                for segment, is_new, update_fields in planned_segments:
                    if is_new:
                        segment.save()
                    else:
                        segment.save(update_fields=update_fields)
                _invalidate_export(project)
                _schedule_storage_cleanup(old_files)

            if import_kind == "video":
                messages.success(
                    request,
                    f"{len(scenes)} clip(s) vidéo ajouté(s). Tu peux les assembler avec leur son d’origine "
                    "ou saisir un texte sur chaque clip pour créer une narration vidéo.",
                )
            else:
                messages.success(request, f"{len(scenes)} scène(s) ajoutée(s) au projet.")
    except ValueError as exc:
        _delete_storage_files(created_files)
        messages.error(request, str(exc))
    except Exception:
        _delete_storage_files(created_files)
        logger.exception("L’import des scènes a échoué pour le projet %s.", project.pk)
        messages.error(request, "L’import a échoué ; aucune scène n’a été enregistrée.")

    return redirect("project_detail", project_id=project.pk)


@login_required
@require_POST
def assemble_videos(request, project_id):
    project = get_object_or_404(NarrationProject, pk=project_id, user=request.user)
    if project.status in {NarrationProject.Status.QUEUED, NarrationProject.Status.PROCESSING}:
        return JsonResponse({"ok": False, "error": "Ce projet est déjà dans la file ou en cours de traitement."}, status=409)
    segments = list(project.segments.order_by("position", "id"))
    if not segments or any(not item.video_clip or item.image for item in segments):
        return JsonResponse({"ok": False, "error": "Ajoute uniquement des clips vidéo à ce projet avant de les assembler."}, status=400)
    project.status = NarrationProject.Status.QUEUED
    project.queued_at = timezone.now()
    project.job_type = NarrationProject.JobType.ASSEMBLY
    project.cancel_requested = False
    project.status_message = "Assemblage vidéo ajouté à la file d’attente."
    project.error_message = ""
    project.progress_percent = 0
    project.current_segment = 0
    project.total_segments = len(segments)
    project.started_at = None
    project.finished_at = None
    project.save(update_fields=[
        "status", "queued_at", "job_type", "cancel_requested", "status_message", "error_message", "progress_percent",
        "current_segment", "total_segments", "started_at", "finished_at", "updated_at",
    ])
    return JsonResponse({"ok": True, "status_url": f"/projects/{project.pk}/status/"})


@login_required
@require_POST
def save_video_options(request, project_id):
    project = get_object_or_404(NarrationProject, pk=project_id, user=request.user)
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


@login_required
@require_POST
def set_project_voice(request, project_id):
    project = get_object_or_404(NarrationProject, pk=project_id, user=request.user)
    voice_id = request.POST.get("voice_id")
    voice = get_object_or_404(Voice, pk=voice_id, user=request.user, consent_confirmed=True) if voice_id else None
    if project.status in {NarrationProject.Status.QUEUED, NarrationProject.Status.PROCESSING}:
        return JsonResponse({"ok": False, "error": "Ce projet est déjà dans la file ou en cours de traitement."}, status=409)
    if project.voice_id != (voice.pk if voice else None):
        project.voice = voice
        _invalidate_export(project)
    return redirect("project_detail", project_id=project.pk)


@login_required
@require_POST
def set_project_music(request, project_id):
    project = get_object_or_404(NarrationProject, pk=project_id, user=request.user)
    if project.status in {NarrationProject.Status.QUEUED, NarrationProject.Status.PROCESSING}:
        return JsonResponse({"ok": False, "error": "Ce projet est déjà dans la file ou en cours de traitement."}, status=409)

    raw_volume = request.POST.get("music_volume", "20")
    try:
        volume = int(raw_volume)
    except (TypeError, ValueError):
        return HttpResponseBadRequest("Le volume de musique doit être un nombre entier entre 0 et 100.")
    if not 0 <= volume <= 100:
        return HttpResponseBadRequest("Le volume de musique doit être compris entre 0 et 100.")

    track_id = (request.POST.get("background_track_id") or "").strip()
    track = get_object_or_404(BackgroundTrack, pk=track_id, user=request.user) if track_id else None
    changed_fields = []
    if project.background_track_id != (track.pk if track else None):
        project.background_track = track
        changed_fields.append("background_track")
    if project.music_volume != volume:
        project.music_volume = volume
        changed_fields.append("music_volume")
    if changed_fields:
        _invalidate_video(project, extra_update_fields=changed_fields)
        messages.success(request, "La musique de fond et son volume ont été enregistrés. La voix en cache est conservée.")
    else:
        messages.info(request, "Les réglages de musique n’ont pas changé.")
    return redirect("project_detail", project_id=project.pk)


@login_required
@require_POST
def add_segment(request, project_id):
    project = get_object_or_404(NarrationProject, pk=project_id, user=request.user)
    if project.status in {NarrationProject.Status.QUEUED, NarrationProject.Status.PROCESSING}:
        return JsonResponse({"ok": False, "error": "Attends la fin du traitement avant de modifier le projet."}, status=409)
    NarrationSegment.objects.create(project=project, position=project.segments.count())
    _invalidate_export(project)
    return redirect("project_detail", project_id=project.pk)


@login_required
@require_POST
def save_segment(request, project_id, segment_id):
    project = get_object_or_404(NarrationProject, pk=project_id, user=request.user)
    segment = get_object_or_404(NarrationSegment, pk=segment_id, project=project)
    if project.status in {NarrationProject.Status.QUEUED, NarrationProject.Status.PROCESSING}:
        return JsonResponse({"ok": False, "error": "Attends la fin du traitement avant de modifier le projet."}, status=409)

    try:
        raw_settings = request.POST.get("animation_settings")
        if raw_settings is None:
            # Compatibility for an already-open page from the previous editor.
            animation_settings = _settings_for_legacy_post(request, segment)
        else:
            animation_settings = _validate_animation_settings_json(raw_settings)
    except ValueError as exc:
        return HttpResponseBadRequest(str(exc))

    text = (request.POST.get("text") or "").strip()[:12000]
    animation_changed = animation_settings != (segment.animation_settings or default_animation_settings())
    text_changed = text != segment.text
    effects = animation_settings["effects"]
    legacy_motion = _legacy_motion_from_animation(animation_settings)
    camera_profile = animation_settings["camera_profile"]
    light_breathing = effects["light_breathing"]["enabled"]

    if text_changed:
        if segment.audio:
            segment.audio.delete(save=False)
        segment.text = text
        segment.audio = ""
        segment.subtitle_data = []
        segment.duration_seconds = 0
        segment.cache_key = ""
    segment.text = text
    segment.animation_settings = animation_settings
    # Keep the previous columns populated for compatibility and safe rollback.
    segment.motion = legacy_motion
    segment.camera_profile = camera_profile
    segment.light_breathing = light_breathing
    segment.save(update_fields=[
        "text", "animation_settings", "motion", "camera_profile", "light_breathing", "audio",
        "subtitle_data", "duration_seconds", "cache_key", "updated_at",
    ])
    if text_changed:
        _invalidate_export(project)
        messages.success(request, "Le segment a été enregistré.")
    elif animation_changed:
        _invalidate_video(project)
        messages.success(request, "Les réglages d’animation ont été enregistrés.")
    else:
        messages.info(request, "Aucun changement à enregistrer.")
    return redirect(f"/projects/{project.pk}/#segment-{segment.pk}")


@login_required
@require_POST
def delete_segment(request, project_id, segment_id):
    project = get_object_or_404(NarrationProject, pk=project_id, user=request.user)
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


@login_required
@require_POST
def reorder_segments(request, project_id):
    project = get_object_or_404(NarrationProject, pk=project_id, user=request.user)
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


@login_required
@require_POST
def delete_project(request, project_id):
    with transaction.atomic():
        project = get_object_or_404(
            NarrationProject.objects.select_for_update(), pk=project_id, user=request.user,
        )
        if project.status == NarrationProject.Status.PROCESSING:
            messages.error(request, "Annule le traitement et attends son arrêt avant de supprimer le projet.")
            return redirect("project_detail", project_id=project.pk)
        files = [project.full_audio, project.subtitles, project.video_file]
        files += [
            file for segment in project.segments.all()
            for file in (segment.audio, segment.image, segment.video_clip)
        ]
        storage_files = [(file.storage, file.name) for file in files if file and file.name]
        project.delete()
        _schedule_storage_cleanup(storage_files)
    messages.success(request, "Le projet a été supprimé.")
    return redirect("home")


@login_required
@require_POST
def cancel_job(request, project_id):
    with transaction.atomic():
        project = get_object_or_404(
            NarrationProject.objects.select_for_update(), pk=project_id, user=request.user,
        )
        if project.status == NarrationProject.Status.QUEUED:
            project.status = NarrationProject.Status.DRAFT
            project.cancel_requested = False
            project.queued_at = None
            project.started_at = None
            project.finished_at = timezone.now()
            project.progress_percent = 0
            project.current_segment = 0
            project.total_segments = 0
            project.status_message = "Traitement annulé avant son démarrage."
            project.error_message = ""
            project.save(update_fields=[
                "status", "cancel_requested", "queued_at", "started_at", "finished_at",
                "progress_percent", "current_segment", "total_segments", "status_message",
                "error_message", "updated_at",
            ])
            return JsonResponse({
                "ok": True, "status": project.status, "cancel_requested": False,
                "message": project.status_message,
            })
        if project.status == NarrationProject.Status.PROCESSING:
            if not project.cancel_requested:
                project.cancel_requested = True
                project.status_message = "Annulation demandée. Arrêt propre du traitement en cours…"
                project.save(update_fields=["cancel_requested", "status_message", "updated_at"])
            return JsonResponse({
                "ok": True, "status": project.status, "cancel_requested": True,
                "message": project.status_message,
            })
        return JsonResponse({
            "ok": False, "error": "Ce projet n’a pas de traitement actif à annuler.",
        }, status=409)


@login_required
@require_POST
def rename_project(request, project_id):
    project = get_object_or_404(NarrationProject, pk=project_id, user=request.user)
    title = (request.POST.get("title") or "").strip()[:120]
    if not title:
        messages.error(request, "Le nom du projet ne peut pas être vide.")
    elif title != project.title:
        project.title = title
        project.save(update_fields=["title", "updated_at"])
        messages.success(request, "Le nom du projet a été mis à jour.")
    return redirect("project_detail", project_id=project.pk)


@login_required
@require_POST
def prepare(request, project_id):
    project = get_object_or_404(NarrationProject, pk=project_id, user=request.user)
    if project.status in {NarrationProject.Status.QUEUED, NarrationProject.Status.PROCESSING}:
        return JsonResponse({"ok": False, "error": "Ce projet est déjà dans la file ou en cours de traitement."}, status=409)
    voice_id = request.POST.get("voice_id") or project.voice_id
    voice = get_object_or_404(Voice, pk=voice_id, user=request.user, consent_confirmed=True) if voice_id else None
    segments = list(project.segments.order_by("position", "id"))
    if not voice:
        return JsonResponse({"ok": False, "error": "Choisis une voix autorisée pour ce projet."}, status=400)
    if not segments or any(not segment.text.strip() for segment in segments):
        return JsonResponse({"ok": False, "error": "Ajoute un texte à chaque segment avant de préparer la narration."}, status=400)
    has_images = any(segment.image for segment in segments)
    has_video_clips = any(segment.video_clip for segment in segments)
    requested_mode = request.POST.get("mode", "")
    audio_only = request.POST.get("audio_only") == "1"
    if audio_only:
        if has_video_clips and any(not segment.video_clip or segment.image for segment in segments):
            return JsonResponse({"ok": False, "error": "La pré-génération audio accepte uniquement des clips vidéo, sans images mélangées."}, status=400)
        job_type = NarrationProject.JobType.AUDIO
    elif has_video_clips:
        if requested_mode != NarrationProject.JobType.VIDEO_NARRATION:
            return JsonResponse({"ok": False, "error": "Choisis « Narration vidéo » pour remplacer le son des clips par la voix XTTS, ou utilise l’assemblage classique."}, status=400)
        if any(not segment.video_clip or segment.image for segment in segments):
            return JsonResponse({"ok": False, "error": "La narration vidéo accepte uniquement des clips vidéo, sans images mélangées."}, status=400)
        job_type = NarrationProject.JobType.VIDEO_NARRATION
    else:
        if requested_mode == NarrationProject.JobType.VIDEO_NARRATION:
            return JsonResponse({"ok": False, "error": "Ajoute des clips vidéo pour utiliser le mode Narration vidéo."}, status=400)
        if has_images and any(not segment.image for segment in segments):
            return JsonResponse({"ok": False, "error": "Ajoute une image à chaque scène avant de générer la vidéo."}, status=400)
        job_type = NarrationProject.JobType.VIDEO if has_images else NarrationProject.JobType.AUDIO
    if project.voice_id != voice.pk:
        project.voice = voice
        _invalidate_export(project)
    project.status = NarrationProject.Status.QUEUED
    project.queued_at = timezone.now()
    project.job_type = job_type
    project.cancel_requested = False
    project.status_message = (
        "Pré-génération audio XTTS/WhisperX ajoutée à la file d’attente."
        if audio_only
        else "Narration vidéo ajoutée à la file d’attente."
        if job_type == NarrationProject.JobType.VIDEO_NARRATION
        else "Vidéo ajoutée à la file d’attente."
        if has_images
        else "Narration ajoutée à la file d’attente."
    )
    project.error_message = ""
    project.progress_percent = 0
    project.current_segment = 0
    project.total_segments = len(segments)
    project.started_at = None
    project.finished_at = None
    project.save()
    return JsonResponse({"ok": True, "status_url": f"/projects/{project.pk}/status/"})


@json_login_required
def project_status(request, project_id):
    project = get_object_or_404(NarrationProject, pk=project_id, user=request.user)
    elapsed = 0
    if project.started_at:
        end = project.finished_at or timezone.now()
        elapsed = max(0, round((end - project.started_at).total_seconds()))
    queued = NarrationProject.objects.filter(user=request.user, status=NarrationProject.Status.QUEUED).order_by("queued_at", "id")
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
        "cancel_requested": project.cancel_requested,
        "audio_url": project.full_audio.url if project.full_audio else "",
        "subtitles_url": project.subtitles.url if project.subtitles else "",
        "video_url": project.video_file.url if project.video_file else "",
    })


@login_required
def segment_subtitles(request, project_id, segment_id):
    project = get_object_or_404(NarrationProject, pk=project_id, user=request.user)
    segment = get_object_or_404(NarrationSegment, pk=segment_id, project=project)
    if not segment.subtitle_data:
        return HttpResponse("Les sous-titres de ce segment ne sont pas prêts.", status=404)
    response = HttpResponse(srt_text([segment]), content_type="application/x-subrip; charset=utf-8")
    response["Content-Disposition"] = f'attachment; filename="segment-{segment.position + 1}.srt"'
    return response
