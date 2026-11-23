import json
import hashlib
import logging
import subprocess
from copy import deepcopy
from functools import wraps
from pathlib import Path
import tempfile
import uuid
from urllib.parse import urlencode

from django.contrib import messages
from django.contrib.auth import login
from django.contrib.auth.decorators import login_required
from django.contrib.auth.forms import UserCreationForm
from django.core.files import File
from django.core.files.base import ContentFile
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Count, Q
from django.db.models.deletion import ProtectedError
from django.http import Http404, HttpResponse, HttpResponseBadRequest, JsonResponse
from django.urls import reverse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_GET, require_http_methods, require_POST
from PIL import Image, UnidentifiedImageError

from .models import (
    ANIMATION_EFFECTS, ANIMATION_LEVEL_CHOICES, AudioAsset, AudioSession, BackgroundTrack, NarrationProject,
    MediaFolder, NarrationSegment, ProjectFolder, StudioBatch, StudioBatchItem, Voice,
    default_animation_settings, legacy_animation_settings,
)
from .services import (
    _voice_sample_digest, cache_key_for, hash_file, normalize_voice_sample, regenerate_audio_asset, srt_text,
)
from .audio_services import (
    MAX_OPERATIONS as MAX_AUDIO_OPERATIONS,
    _delete_audio_session_files,
    apply_operations as apply_audio_operations,
    available_effects as available_audio_effects,
    available_presets as available_audio_presets,
    cleanup_expired_audio_sessions,
    probe_audio,
    render_pad_sequence,
)
from .video_services import _probe_clip, validate_assembly_project

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
MAX_AUDIO_STUDIO_UPLOAD_BYTES = MAX_AUDIO_ASSET_BYTES
MAX_PAD_SEQUENCE_EVENTS = 500
MAX_STUDIO_BATCH_ITEMS = 20
MAX_STUDIO_TEXT_CHARS = 12000
ALLOWED_MUSIC_EXTENSIONS = {".mp3", ".wav", ".m4a", ".ogg"}
ALLOWED_AUDIO_ASSET_EXTENSIONS = {".mp3", ".wav", ".m4a", ".ogg"}
ALLOWED_SAMPLE_EXTENSIONS = {".wav", ".mp3", ".webm", ".m4a", ".ogg"}
ALLOWED_VIDEO_EXTENSIONS = {".mp4", ".mov", ".m4v", ".webm"}
ANIMATION_EFFECT_DEFINITIONS = (
    {"key": "zoom", "label": "Zoom", "directions": (("in", "Avant"), ("out", "Arrière"))},
    {"key": "pan", "label": "Panoramique", "directions": (
        ("horizontal", "Panoramique gauche → droite"), ("horizontal_reverse", "Panoramique droite → gauche"),
        ("vertical", "Panoramique haut → bas"), ("vertical_reverse", "Panoramique bas → haut"),
        ("diagonal", "Diagonale ↘"), ("diagonal_reverse", "Diagonale ↖"),
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
        "pan": {
            "horizontal", "horizontal_reverse", "vertical", "vertical_reverse",
            "diagonal", "diagonal_reverse",
        },
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


def _assembly_motion_from_animation(settings):
    effects = settings["effects"]
    if effects["parallax"]["enabled"]:
        return NarrationSegment.AssemblyMotion.PARALLAX
    zoom = effects["zoom"]
    pan = effects["pan"]
    if zoom["enabled"]:
        return (
            NarrationSegment.AssemblyMotion.ZOOM_OUT
            if zoom["direction"] == "out"
            else NarrationSegment.AssemblyMotion.ZOOM_IN
        )
    if pan["enabled"]:
        return (
            NarrationSegment.AssemblyMotion.PAN_LEFT
            if pan["direction"] in {"horizontal_reverse", "vertical_reverse", "diagonal_reverse"}
            else NarrationSegment.AssemblyMotion.PAN_RIGHT
        )
    return NarrationSegment.AssemblyMotion.NONE


def _animation_settings_for_assembly_motion(motion):
    settings = default_assembly_animation_settings()
    effects = settings["effects"]
    if motion == NarrationSegment.AssemblyMotion.ZOOM_IN:
        effects["zoom"].update(enabled=True, direction="in")
    elif motion == NarrationSegment.AssemblyMotion.ZOOM_OUT:
        effects["zoom"].update(enabled=True, direction="out")
    elif motion == NarrationSegment.AssemblyMotion.PAN_LEFT:
        effects["pan"].update(enabled=True, direction="horizontal_reverse")
    elif motion == NarrationSegment.AssemblyMotion.PAN_RIGHT:
        effects["pan"].update(enabled=True, direction="horizontal")
    elif motion == NarrationSegment.AssemblyMotion.PARALLAX:
        effects["parallax"]["enabled"] = True
    return settings


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


def _safe_delete_media_file(file_field):
    """Delete a stored file only after commit and only when no scene shares it.

    ``file_field`` may be a Django FieldFile or a captured ``(storage, name)``
    pair. Physical deletion is deliberately centralized here so duplicated
    projects can safely share image/video paths.
    """
    if isinstance(file_field, tuple):
        storage, name = file_field
    else:
        storage, name = file_field.storage, file_field.name
    if not name:
        return

    if NarrationSegment.objects.filter(Q(image=name) | Q(video_clip=name)).exists():
        logger.debug("Conservation du média partagé %s : il est encore référencé.", name)
        return

    try:
        storage.delete(name)
    except FileNotFoundError:
        logger.warning("Le fichier média %s était déjà absent lors du nettoyage.", name)
    except Exception:
        logger.exception("Impossible de supprimer le fichier média %s.", name)


def _delete_storage_files(files):
    for file_field in files:
        _safe_delete_media_file(file_field)


def _schedule_storage_cleanup(files):
    files_to_delete = tuple(files)
    if files_to_delete:
        # Storage deletion runs only after the DB commit, so a rollback cannot
        # restore a reference to a file that has already been removed.
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


def _get_page(queryset, page_number, per_page):
    paginator = Paginator(queryset, per_page)
    try:
        requested_page = int(page_number)
    except (TypeError, ValueError):
        requested_page = 1
    return paginator.get_page(max(requested_page, 1))


def get_per_page(request, default=20):
    """Return the requested list size when it is one of the supported choices."""
    try:
        value = int(request.GET.get("per_page", default))
    except (TypeError, ValueError):
        return default
    return value if value in {5, 10, 20} else default


def _pagination_params(request, *, preserve=(), default=20):
    params = {key: request.GET.get(key) for key in preserve if key in request.GET}
    params["per_page"] = str(get_per_page(request, default=default))
    return urlencode(params)


def _filter_media_folder(request, queryset):
    folder_active = "folder" in request.GET
    folder_value = request.GET.get("folder") if folder_active else None
    selected_folder = None
    if folder_active:
        if folder_value == "":
            queryset = queryset.filter(folder__isnull=True)
        else:
            try:
                folder_id = int(folder_value)
            except (TypeError, ValueError):
                raise Http404
            selected_folder = get_object_or_404(MediaFolder, pk=folder_id, user=request.user)
            queryset = queryset.filter(folder=selected_folder)
    return queryset, {
        "media_folder_filter_active": folder_active,
        "media_folder_filter": folder_value,
        "selected_media_folder": selected_folder,
        "media_folders": MediaFolder.objects.filter(user=request.user).order_by("name", "id"),
    }


def _page_or_redirect(request, queryset, per_page):
    page = _get_page(queryset, request.GET.get("page"), per_page)
    requested_page = request.GET.get("page")
    if requested_page is not None and requested_page != str(page.number):
        query = request.GET.copy()
        query["page"] = str(page.number)
        return None, redirect(f"{request.path}?{query.urlencode()}")
    return page, None


@login_required
def home(request):
    query = (request.GET.get("q") or "").strip()[:80]
    category = request.GET.get("category", "")
    voices = Voice.objects.filter(user=request.user)
    if query:
        voices = voices.filter(name__icontains=query)
    if category in Voice.Category.values:
        voices = voices.filter(category=category)
    per_page = get_per_page(request, default=10)
    projects = NarrationProject.objects.filter(user=request.user)
    project_page, page_response = _page_or_redirect(request, projects, per_page)
    if page_response:
        return page_response
    return render(request, "studio/home.html", {
        "voices": voices,
        "projects": project_page.object_list,
        "project_page": project_page,
        "voice_count": Voice.objects.filter(user=request.user).count(),
        "project_count": NarrationProject.objects.filter(user=request.user).count(),
        "project_per_page": per_page,
        "pagination_params": _pagination_params(request, preserve=("q", "category"), default=10),
        "query": query,
        "selected_category": category,
        "categories": Voice.Category.choices,
    })


@login_required
def project_list(request):
    projects = NarrationProject.objects.filter(user=request.user).annotate(
        segment_count=Count("segments"),
    ).order_by("-updated_at", "-id")
    folder_filter_active = "folder" in request.GET
    folder_filter = request.GET.get("folder") if folder_filter_active else None
    selected_folder = None
    if folder_filter_active:
        if folder_filter == "":
            projects = projects.filter(folder__isnull=True)
        else:
            try:
                folder_id = int(folder_filter)
            except (TypeError, ValueError):
                raise Http404
            selected_folder = get_object_or_404(ProjectFolder, pk=folder_id, user=request.user)
            projects = projects.filter(folder=selected_folder)
    per_page = get_per_page(request)
    project_page, page_response = _page_or_redirect(request, projects, per_page)
    if page_response:
        return page_response
    return render(request, "studio/projects.html", {
        "project_page": project_page,
        "project_total": project_page.paginator.count,
        "projects": project_page.object_list,
        "project_per_page": per_page,
        "pagination_params": _pagination_params(request, preserve=("folder",)),
        "folder_filter_active": folder_filter_active,
        "folder_filter": folder_filter,
        "selected_folder": selected_folder,
        "project_folders": ProjectFolder.objects.filter(user=request.user).order_by("name", "id"),
    })


def _folder_action_response(request, *, message, status=200, folder_id=None):
    if request.headers.get("x-requested-with") == "XMLHttpRequest":
        return JsonResponse({"ok": status < 400, "message": message, "folder_id": folder_id}, status=status)
    if status >= 400:
        return HttpResponseBadRequest(message)
    return redirect("project_list")


@login_required
@require_POST
def create_folder(request):
    name = (request.POST.get("name") or "").strip()
    if not name:
        return _folder_action_response(request, message="Le nom du dossier est obligatoire.", status=400)
    if len(name) > 120:
        return _folder_action_response(request, message="Le nom du dossier ne peut pas dépasser 120 caractères.", status=400)
    folder = ProjectFolder.objects.create(user=request.user, name=name)
    messages.success(request, f"Dossier « {folder.name} » créé.")
    return _folder_action_response(
        request, message="Dossier créé.", folder_id=folder.pk,
    )


@login_required
@require_POST
def rename_folder(request, folder_id):
    folder = get_object_or_404(ProjectFolder, pk=folder_id, user=request.user)
    name = (request.POST.get("name") or "").strip()
    if not name:
        return _folder_action_response(request, message="Le nom du dossier est obligatoire.", status=400)
    if len(name) > 120:
        return _folder_action_response(request, message="Le nom du dossier ne peut pas dépasser 120 caractères.", status=400)
    if folder.name != name:
        folder.name = name
        folder.save(update_fields=["name"])
        messages.success(request, f"Dossier renommé : « {folder.name} ».")
    return _folder_action_response(request, message="Dossier renommé.", folder_id=folder.pk)


@login_required
@require_POST
def delete_folder(request, folder_id):
    folder = get_object_or_404(ProjectFolder, pk=folder_id, user=request.user)
    if request.POST.get("confirm") != "1":
        return _folder_action_response(request, message="Confirmation manquante.", status=400)
    name = folder.name
    folder.delete()
    messages.success(request, f"Dossier « {name} » supprimé. Ses projets sont à la racine.")
    if request.headers.get("x-requested-with") == "XMLHttpRequest":
        return JsonResponse({"success": True, "redirect_url": reverse("project_list")})
    return _folder_action_response(request, message="Dossier supprimé.", folder_id=folder_id)


@login_required
@require_POST
def move_project(request, project_id):
    project = get_object_or_404(NarrationProject, pk=project_id, user=request.user)
    raw_folder_id = request.POST.get("folder_id", "")
    folder = None
    if raw_folder_id:
        try:
            folder_id = int(raw_folder_id)
        except (TypeError, ValueError):
            return JsonResponse({"ok": False, "error": "Dossier invalide."}, status=400)
        folder = get_object_or_404(ProjectFolder, pk=folder_id, user=request.user)

    if project.folder_id != (folder.pk if folder else None):
        project.folder = folder
        project.save(update_fields=["folder", "updated_at"])
        messages.success(request, "Projet déplacé.")
    response = {"ok": True, "folder_id": folder.pk if folder else None}
    if request.headers.get("x-requested-with") == "XMLHttpRequest":
        return JsonResponse(response)
    return redirect(_safe_return_url(request, reverse("project_list")))


def _media_folder_action_response(request, *, message, status=200, folder_id=None):
    if request.headers.get("x-requested-with") == "XMLHttpRequest":
        payload = {"ok": status < 400, "message": message, "folder_id": folder_id}
        if status >= 400:
            payload["error"] = message
        return JsonResponse(payload, status=status)
    if status >= 400:
        return HttpResponseBadRequest(message)
    return redirect(_safe_return_url(request, reverse("audios")))


@login_required
@require_POST
def create_media_folder(request):
    name = (request.POST.get("name") or "").strip()
    if not name:
        return _media_folder_action_response(request, message="Le nom du dossier est obligatoire.", status=400)
    if len(name) > 120:
        return _media_folder_action_response(
            request, message="Le nom du dossier ne peut pas dépasser 120 caractères.", status=400,
        )
    folder = MediaFolder.objects.create(user=request.user, name=name)
    messages.success(request, f"Dossier média « {folder.name} » créé.")
    return _media_folder_action_response(request, message="Dossier média créé.", folder_id=folder.pk)


@login_required
@require_POST
def rename_media_folder(request, folder_id):
    folder = get_object_or_404(MediaFolder, pk=folder_id, user=request.user)
    name = (request.POST.get("name") or "").strip()
    if not name:
        return _media_folder_action_response(request, message="Le nom du dossier est obligatoire.", status=400)
    if len(name) > 120:
        return _media_folder_action_response(
            request, message="Le nom du dossier ne peut pas dépasser 120 caractères.", status=400,
        )
    if folder.name != name:
        folder.name = name
        folder.save(update_fields=["name"])
        messages.success(request, f"Dossier média renommé : « {folder.name} ».")
    return _media_folder_action_response(request, message="Dossier média renommé.", folder_id=folder.pk)


@login_required
@require_POST
def delete_media_folder(request, folder_id):
    folder = get_object_or_404(MediaFolder, pk=folder_id, user=request.user)
    if request.POST.get("confirm") != "1":
        return _media_folder_action_response(request, message="Confirmation manquante.", status=400)
    name = folder.name
    folder.delete()
    messages.success(request, f"Dossier média « {name} » supprimé. Ses médias sont à la racine.")
    redirect_url = _safe_return_url(request, reverse("audios"))
    if request.headers.get("x-requested-with") == "XMLHttpRequest":
        return JsonResponse({"success": True, "redirect_url": redirect_url})
    return redirect(redirect_url)


def _media_move_target(request):
    raw_folder_id = request.POST.get("folder_id", "")
    if not raw_folder_id:
        return None
    try:
        folder_id = int(raw_folder_id)
    except (TypeError, ValueError):
        return JsonResponse({"ok": False, "error": "Dossier média invalide."}, status=400)
    return get_object_or_404(MediaFolder, pk=folder_id, user=request.user)


@login_required
@require_POST
def move_audio_asset(request, asset_id):
    asset = get_object_or_404(AudioAsset, pk=asset_id, user=request.user)
    folder = _media_move_target(request)
    if isinstance(folder, JsonResponse):
        return folder
    if asset.folder_id != (folder.pk if folder else None):
        asset.folder = folder
        asset.save(update_fields=["folder", "updated_at"])
        messages.success(request, f"Audio « {asset.name} » déplacé.")
    response = {"ok": True, "folder_id": folder.pk if folder else None}
    if request.headers.get("x-requested-with") == "XMLHttpRequest":
        return JsonResponse(response)
    return redirect(_safe_return_url(request, reverse("audios")))


@login_required
@require_POST
def move_music(request, track_id):
    track = get_object_or_404(BackgroundTrack, pk=track_id, user=request.user)
    folder = _media_move_target(request)
    if isinstance(folder, JsonResponse):
        return folder
    if track.folder_id != (folder.pk if folder else None):
        track.folder = folder
        track.save(update_fields=["folder", "updated_at"])
        messages.success(request, f"Musique « {track.name} » déplacée.")
    response = {"ok": True, "folder_id": folder.pk if folder else None}
    if request.headers.get("x-requested-with") == "XMLHttpRequest":
        return JsonResponse(response)
    return redirect(_safe_return_url(request, reverse("musics")))


@login_required
def musics(request):
    per_page = get_per_page(request)
    tracks, folder_context = _filter_media_folder(
        request, BackgroundTrack.objects.filter(user=request.user),
    )
    music_page, page_response = _page_or_redirect(
        request, tracks, per_page,
    )
    if page_response:
        return page_response
    context = {
        "background_tracks": music_page.object_list,
        "music_page": music_page,
        "music_per_page": per_page,
        "pagination_params": _pagination_params(request, preserve=("folder",)),
        "projects": NarrationProject.objects.filter(user=request.user)[:30],
    }
    context.update(folder_context)
    return render(request, "studio/musics.html", context)


def _audio_studio_signature(operations):
    payload = json.dumps(operations, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:20]


def _audio_studio_preview_is_current(session):
    if not session.current_preview_file or not session.current_preview_file.name:
        return False
    signature = _audio_studio_signature(session.operations or [])
    return session.current_preview_file.name.endswith(f"_{signature}.m4a")


def _audio_studio_editor_url(session, source_name=""):
    url = reverse("audio_studio_edit", args=[session.pk])
    return f"{url}?{urlencode({'source_name': source_name})}" if source_name else url


@login_required
def audio_studio(request):
    try:
        cleanup_expired_audio_sessions(user=request.user)
    except Exception:
        logger.warning("Nettoyage opportuniste des sessions audio échoué pour l’utilisateur %s.", request.user.pk, exc_info=True)
    return render(request, "studio/audio_studio.html", {
        "background_tracks": BackgroundTrack.objects.filter(user=request.user).order_by("name", "id"),
        "audio_assets": AudioAsset.objects.filter(user=request.user).order_by("-created_at", "-id"),
        "audio_sessions": AudioSession.objects.filter(user=request.user).order_by("-updated_at", "-id"),
        "projects": NarrationProject.objects.filter(user=request.user)[:30],
        "max_audio_studio_upload_mb": MAX_AUDIO_STUDIO_UPLOAD_BYTES // (1024 * 1024),
    })


@login_required
@require_POST
def create_audio_studio_session(request):
    source_kind = request.POST.get("source_kind")
    source_name = ""
    session = AudioSession(user=request.user, source_kind=source_kind or "")
    if source_kind == AudioSession.SourceKind.UPLOAD:
        upload = request.FILES.get("audio_file")
        if not upload or upload.size <= 0:
            messages.error(request, "Sélectionne un fichier audio.")
            return redirect("audio_studio")
        if upload.size > MAX_AUDIO_STUDIO_UPLOAD_BYTES:
            messages.error(request, "Le fichier dépasse la limite de 100 Mo.")
            return redirect("audio_studio")
        if Path(upload.name).suffix.lower() not in ALLOWED_AUDIO_ASSET_EXTENSIONS:
            messages.error(request, "Formats acceptés : MP3, WAV, M4A et OGG.")
            return redirect("audio_studio")
        source_name = Path(upload.name).stem.strip() or "Audio importé"
        source_filename = Path(upload.name).name
        source_file = upload
    elif source_kind == AudioSession.SourceKind.FROM_MUSIC:
        track_id = request.POST.get("source_id")
        track = get_object_or_404(BackgroundTrack, pk=track_id, user=request.user)
        if not track.audio_file:
            messages.error(request, "Le fichier de cette musique est introuvable.")
            return redirect("audio_studio")
        source_name = track.name
        source_filename = f"{source_name}{Path(track.audio_file.name).suffix.lower()}"
        source_file = track.audio_file
    elif source_kind == AudioSession.SourceKind.FROM_AUDIO:
        asset_id = request.POST.get("source_id")
        asset = get_object_or_404(AudioAsset, pk=asset_id, user=request.user)
        if not asset.audio_file:
            messages.error(request, "Le fichier de cet audio est introuvable.")
            return redirect("audio_studio")
        source_name = asset.name
        source_filename = f"{source_name}{Path(asset.audio_file.name).suffix.lower()}"
        source_file = asset.audio_file
    else:
        return HttpResponseBadRequest("Source audio invalide.")

    try:
        if source_kind == AudioSession.SourceKind.UPLOAD:
            session.source_file.save(source_filename, source_file, save=False)
        else:
            with source_file.open("rb") as stream:
                session.source_file.save(source_filename, File(stream), save=False)
        session.save()
    except Exception:
        if session.source_file and session.source_file.name:
            session.source_file.storage.delete(session.source_file.name)
        logger.exception("Création de session audio impossible pour l’utilisateur %s.", request.user.pk)
        messages.error(request, "Impossible de préparer ce fichier audio.")
        return redirect("audio_studio")
    return redirect(_audio_studio_editor_url(session, source_name))


@login_required
def audio_studio_edit(request, session_id):
    session = get_object_or_404(AudioSession, pk=session_id, user=request.user)
    source_name = (request.GET.get("source_name") or Path(session.source_file.name).stem).strip()
    preview_exists = bool(
        session.current_preview_file and session.current_preview_file.name
        and session.current_preview_file.storage.exists(session.current_preview_file.name)
    )
    preview_missing = bool(
        session.current_preview_file and session.current_preview_file.name and not preview_exists
    )
    return render(request, "studio/audio_studio_editor.html", {
        "session": session,
        "source_name": source_name,
        "default_track_name": f"Édition de {source_name}"[:120],
        "preview_exists": preview_exists,
        "preview_missing": preview_missing,
        "preview_is_current": preview_exists and _audio_studio_preview_is_current(session),
        "effects": available_audio_effects(),
        "presets": available_audio_presets(),
        "projects": NarrationProject.objects.filter(user=request.user)[:30],
    })


@login_required
@require_POST
def audio_studio_operations(request, session_id):
    session = get_object_or_404(AudioSession, pk=session_id, user=request.user)
    try:
        payload = json.loads(request.body or b"{}")
    except (json.JSONDecodeError, UnicodeDecodeError):
        return JsonResponse({"error": "Requête audio invalide."}, status=400)
    if not isinstance(payload, dict):
        return JsonResponse({"error": "Requête audio invalide."}, status=400)

    operations = list(session.operations or [])
    action = payload.get("action")
    if action == "add":
        operation = payload.get("operation")
        if (not isinstance(operation, dict) or set(operation) != {"type", "params"}
                or operation.get("type") not in {"trim", "fade", "volume", "effect", "preset"}
                or not isinstance(operation.get("params"), dict)):
            return JsonResponse({"error": "Opération audio invalide."}, status=400)
        if len(operations) >= MAX_AUDIO_OPERATIONS:
            return JsonResponse({"error": f"Limite de {MAX_AUDIO_OPERATIONS} opérations atteinte."}, status=400)
        operations.append(operation)
    elif action == "remove":
        try:
            index = int(payload.get("index"))
            if index < 0:
                raise ValueError
            operations.pop(index)
        except (TypeError, ValueError, IndexError):
            return JsonResponse({"error": "Opération introuvable."}, status=400)
    elif action == "undo":
        if operations:
            operations.pop()
    elif action == "reset":
        operations = []
    else:
        return JsonResponse({"error": "Action audio inconnue."}, status=400)

    session.operations = operations
    session.save(update_fields=["operations", "updated_at"])
    preview_exists = bool(session.current_preview_file and session.current_preview_file.name)
    return JsonResponse({
        "operations": operations,
        "has_preview": preview_exists,
        "preview_valid": preview_exists and _audio_studio_preview_is_current(session),
    })


@login_required
@require_POST
def audio_studio_preview(request, session_id):
    # Refresh activity while briefly holding the row lock. Once this commits,
    # the expiry cleaner will not remove the source while FFmpeg is using it.
    with transaction.atomic():
        session = get_object_or_404(
            AudioSession.objects.select_for_update(), pk=session_id, user=request.user,
        )
        if not session.source_file or not session.source_file.name:
            return JsonResponse({"error": "Le fichier source est introuvable."}, status=400)
        source_path = session.source_file.path
        operations = list(session.operations or [])
        session.updated_at = timezone.now()
        session.save(update_fields=["updated_at"])
    signature = _audio_studio_signature(operations)
    try:
        with tempfile.TemporaryDirectory(prefix="petit-studio-audio-preview-") as folder:
            output_path = Path(folder) / "preview.m4a"
            _result_path, duration = apply_audio_operations(source_path, operations, output_path)
            storage = session.current_preview_file.storage
            target_name = (
                f"audio-sessions/user_{request.user.pk}/session_{session.pk}_"
                f"{uuid.uuid4().hex}_{signature}.m4a"
            )
            with output_path.open("rb") as stream:
                stored_name = storage.save(target_name, File(stream))

        try:
            with transaction.atomic():
                current = get_object_or_404(
                    AudioSession.objects.select_for_update(), pk=session_id, user=request.user,
                )
                if _audio_studio_signature(current.operations or []) != signature:
                    storage.delete(stored_name)
                    return JsonResponse({"error": "Les opérations ont changé pendant l’aperçu. Relance le rendu."}, status=409)
                current.current_preview_file.name = stored_name
                current.save(update_fields=["current_preview_file", "updated_at"])
                session = current
        except Exception:
            storage.delete(stored_name)
            raise
        return JsonResponse({"url": session.current_preview_file.url, "duration": duration})
    except (OSError, ValueError, RuntimeError, TimeoutError, subprocess.SubprocessError) as exc:
        logger.warning("Aperçu audio échoué pour la session %s : %s", session_id, exc)
        return JsonResponse({"error": f"Traitement audio échoué : {exc}"}, status=400)


@login_required
@require_POST
def audio_studio_save(request, session_id):
    name = (request.POST.get("name") or "").strip()
    session = get_object_or_404(AudioSession, pk=session_id, user=request.user)
    if not name:
        messages.error(request, "Le nom de la musique est obligatoire.")
        return redirect(_audio_studio_editor_url(session, request.POST.get("source_name", "")))
    if len(name) > 120:
        messages.error(request, "Le nom ne peut pas dépasser 120 caractères.")
        return redirect(_audio_studio_editor_url(session, request.POST.get("source_name", "")))
    if not _audio_studio_preview_is_current(session):
        messages.error(request, "Les opérations ont changé depuis le dernier aperçu. Clique à nouveau sur Écouter pour valider.")
        return redirect(_audio_studio_editor_url(session, request.POST.get("source_name", "")))
    if not session.current_preview_file.storage.exists(session.current_preview_file.name):
        messages.error(request, "Le fichier d’aperçu est introuvable. Relance l’aperçu.")
        return redirect(_audio_studio_editor_url(session, request.POST.get("source_name", "")))

    track = BackgroundTrack(user=request.user, name=name, duration=0)
    created_file = None
    try:
        with transaction.atomic():
            session = get_object_or_404(
                AudioSession.objects.select_for_update(), pk=session_id, user=request.user,
            )
            if not _audio_studio_preview_is_current(session):
                messages.error(request, "Les opérations ont changé depuis le dernier aperçu. Clique à nouveau sur Écouter pour valider.")
                return redirect(_audio_studio_editor_url(session, request.POST.get("source_name", "")))
            duration, _sample_rate = probe_audio(session.current_preview_file.path)
            track.duration = duration
            with session.current_preview_file.open("rb") as stream:
                track.audio_file.save(f"{name}.m4a", File(stream), save=False)
            created_file = (track.audio_file.storage, track.audio_file.name)
            track.save()
            _delete_audio_session_files(session)
            session.delete()
    except Exception:
        if created_file:
            try:
                created_file[0].delete(created_file[1])
            except Exception:
                logger.warning("Nettoyage du fichier musique échoué après erreur de sauvegarde.", exc_info=True)
        logger.exception("Sauvegarde de la session audio %s échouée.", session_id)
        messages.error(request, "Impossible de sauvegarder cette musique. La session est conservée.")
        return redirect(_audio_studio_editor_url(session, request.POST.get("source_name", "")))
    messages.success(request, f"Musique ajoutée : {track.name}")
    return redirect("musics")


@login_required
@require_POST
def audio_studio_save_sequence(request, session_id):
    session = get_object_or_404(AudioSession, pk=session_id, user=request.user)
    if request.content_type != "application/json" or len(request.body) > 200_000:
        return JsonResponse({"error": "Requête de séquence invalide ou trop volumineuse."}, status=400)
    try:
        payload = json.loads(request.body or b"{}")
    except (json.JSONDecodeError, UnicodeDecodeError):
        return JsonResponse({"error": "Le JSON de la séquence est invalide."}, status=400)
    if not isinstance(payload, dict) or set(payload) != {"name", "sequence_data", "pad_boundaries"}:
        return JsonResponse({"error": "Les données de la composition sont incomplètes."}, status=400)

    name = payload.get("name")
    if not isinstance(name, str) or not name.strip():
        return JsonResponse({"error": "Donne un nom à la composition."}, status=400)
    name = name.strip()
    if len(name) > 120:
        return JsonResponse({"error": "Le nom ne peut pas dépasser 120 caractères."}, status=400)

    sequence_data = payload.get("sequence_data")
    if (not isinstance(sequence_data, dict)
            or set(sequence_data) != {"version", "recording_duration_ms", "bpm", "events"}
            or type(sequence_data.get("version")) is not int
            or sequence_data.get("version") != 1):
        return JsonResponse({"error": "Le format de la séquence n’est pas valide."}, status=400)
    bpm = sequence_data.get("bpm")
    if isinstance(bpm, bool) or not isinstance(bpm, int) or not 60 <= bpm <= 200:
        return JsonResponse({"error": "Le BPM doit être un entier compris entre 60 et 200."}, status=400)
    events = sequence_data.get("events")
    if not isinstance(events, list) or not events:
        return JsonResponse({"error": "Enregistre au moins un pad avant de sauvegarder."}, status=400)
    if len(events) > MAX_PAD_SEQUENCE_EVENTS:
        return JsonResponse({"error": f"La séquence est limitée à {MAX_PAD_SEQUENCE_EVENTS} déclenchements."}, status=400)
    recording_duration_ms = sequence_data.get("recording_duration_ms")
    if (isinstance(recording_duration_ms, bool) or not isinstance(recording_duration_ms, int)
            or recording_duration_ms < 1):
        return JsonResponse({"error": "La durée d’enregistrement est invalide."}, status=400)
    for event in events:
        if not isinstance(event, dict) or set(event) != {"pad_index", "timestamp_ms"}:
            return JsonResponse({"error": "Un événement de pad est invalide."}, status=400)
        pad_index, timestamp_ms = event["pad_index"], event["timestamp_ms"]
        if isinstance(pad_index, bool) or not isinstance(pad_index, int) or not 0 <= pad_index <= 15:
            return JsonResponse({"error": "Un indice de pad doit être compris entre 0 et 15."}, status=400)
        if (isinstance(timestamp_ms, bool) or not isinstance(timestamp_ms, int)
                or not 0 <= timestamp_ms <= recording_duration_ms):
            return JsonResponse({"error": "Un horodatage de pad est invalide."}, status=400)
    pad_boundaries = payload.get("pad_boundaries")
    if not isinstance(pad_boundaries, list) or len(pad_boundaries) != 16:
        return JsonResponse({"error": "Les 16 bornes des pads sont requises."}, status=400)

    created_file = None
    try:
        with transaction.atomic():
            session = get_object_or_404(
                AudioSession.objects.select_for_update(), pk=session_id, user=request.user,
            )
            if not session.source_file or not session.source_file.name:
                return JsonResponse({"error": "Le fichier source est introuvable."}, status=400)
            source_path = session.source_file.path
            session.sequencer_data = sequence_data
            session.updated_at = timezone.now()
            session.save(update_fields=["sequencer_data", "updated_at"])

        track = BackgroundTrack(user=request.user, name=name, duration=0)
        with tempfile.TemporaryDirectory(prefix="petit-studio-pad-sequence-") as folder:
            output_path = Path(folder) / "composition.m4a"
            _result_path, duration = render_pad_sequence(
                source_path, sequence_data, pad_boundaries, output_path,
            )
            track.duration = duration
            with transaction.atomic():
                current = get_object_or_404(
                    AudioSession.objects.select_for_update(), pk=session_id, user=request.user,
                )
                if current.sequencer_data != sequence_data:
                    return JsonResponse({"error": "La séquence a changé. Recharge la page avant de réessayer."}, status=409)
                with output_path.open("rb") as stream:
                    track.audio_file.save("composition.m4a", File(stream), save=False)
                created_file = (track.audio_file.storage, track.audio_file.name)
                track.save()
                _delete_audio_session_files(current)
                current.delete()
        messages.success(request, f"Composition ajoutée : {track.name}")
        return JsonResponse({"redirect_url": reverse("musics")})
    except Http404:
        raise
    except (OSError, ValueError, RuntimeError, TimeoutError, subprocess.SubprocessError) as exc:
        logger.warning("Rendu de composition échoué pour la session %s : %s", session_id, exc)
        return JsonResponse({"error": f"Impossible de créer la composition : {exc}"}, status=400)
    except Exception:
        if created_file:
            try:
                created_file[0].delete(created_file[1])
            except Exception:
                logger.warning("Nettoyage du fichier de composition échoué.", exc_info=True)
        logger.exception("Sauvegarde de composition échouée pour la session %s.", session_id)
        return JsonResponse({"error": "Impossible de sauvegarder la composition. La session est conservée."}, status=500)


def _studio_page_context(request, *, texts=None, selected_voice_id="", name_prefix="", studio_page=None, per_page=20):
    if studio_page is None:
        studio_page = _get_page(
            StudioBatch.objects.filter(user=request.user)
            .exclude(status__in=[StudioBatch.Status.PENDING, StudioBatch.Status.PROCESSING])
            .select_related("voice").prefetch_related(
                "items__audio_asset",
            ),
            request.GET.get("page"), per_page,
        )
    return {
        "voices": Voice.objects.filter(user=request.user, consent_confirmed=True),
        "studio_batches": studio_page.object_list,
        "active_studio_batches": StudioBatch.objects.filter(
            user=request.user,
            status__in=[StudioBatch.Status.PENDING, StudioBatch.Status.PROCESSING],
        ).select_related("voice").prefetch_related("items__audio_asset"),
        "studio_page": studio_page,
        "studio_per_page": per_page,
        "pagination_params": _pagination_params(request),
        "projects": NarrationProject.objects.filter(user=request.user)[:30],
        "texts": texts or [""],
        "selected_voice_id": str(selected_voice_id),
        "name_prefix": name_prefix,
        "max_batch_items": MAX_STUDIO_BATCH_ITEMS,
        "max_text_chars": MAX_STUDIO_TEXT_CHARS,
    }


@login_required
def studio_home(request):
    batches = StudioBatch.objects.filter(user=request.user).exclude(
        status__in=[StudioBatch.Status.PENDING, StudioBatch.Status.PROCESSING],
    ).select_related("voice").prefetch_related("items__audio_asset")
    per_page = get_per_page(request)
    studio_page, page_response = _page_or_redirect(request, batches, per_page)
    if page_response:
        return page_response
    return render(request, "studio/studio.html", _studio_page_context(request, studio_page=studio_page, per_page=per_page))


@login_required
@require_POST
def cancel_studio_batch(request, batch_id):
    with transaction.atomic():
        batch = get_object_or_404(
            StudioBatch.objects.select_for_update(), pk=batch_id, user=request.user,
        )
        if batch.status not in {StudioBatch.Status.PENDING, StudioBatch.Status.PROCESSING}:
            messages.error(request, "Ce lot ne peut plus être annulé.")
            return redirect("studio_home")

        was_pending = batch.status == StudioBatch.Status.PENDING
        batch.status = StudioBatch.Status.CANCELLED
        batch.status_message = (
            "Lot annulé · les narrations déjà terminées sont conservées."
            if was_pending else
            "Annulation demandée · la narration en cours va se terminer avant l’arrêt du lot."
        )
        batch.queued_at = None
        if was_pending:
            batch.finished_at = timezone.now()
            batch.items.filter(status=StudioBatchItem.Status.PENDING).update(
                status=StudioBatchItem.Status.CANCELLED,
                updated_at=timezone.now(),
            )
        batch.save(update_fields=["status", "status_message", "queued_at", "finished_at", "updated_at"])

    messages.success(
        request,
        "Le lot a été annulé." if was_pending else
        "Annulation demandée. La narration en cours se terminera avant l’arrêt du lot.",
    )
    return redirect("studio_home")


@login_required
@require_POST
def create_studio_batch(request):
    raw_texts = request.POST.getlist("texts")
    texts = [text.strip() for text in raw_texts]
    voice_id = request.POST.get("voice_id", "")
    name_prefix = (request.POST.get("name_prefix") or "").strip()
    error = None

    if not texts:
        error = "Ajoute au moins une narration."
    elif len(texts) > MAX_STUDIO_BATCH_ITEMS:
        error = f"Un lot peut contenir au maximum {MAX_STUDIO_BATCH_ITEMS} narrations."
    elif any(not text for text in texts):
        error = "Chaque narration doit contenir du texte."
    elif any(len(text) > MAX_STUDIO_TEXT_CHARS for text in texts):
        error = f"Chaque texte doit faire au maximum {MAX_STUDIO_TEXT_CHARS} caractères."
    elif len(name_prefix) > 100:
        error = "Le nom de base ne peut pas dépasser 100 caractères."

    try:
        voice_id = int(voice_id)
    except (TypeError, ValueError):
        voice_id = None
    voice = Voice.objects.filter(
        pk=voice_id, user=request.user, consent_confirmed=True,
    ).first() if voice_id else None
    if error is None and voice is None:
        error = "Choisis une voix de ta bibliothèque."

    if error:
        messages.error(request, error)
        return render(request, "studio/studio.html", _studio_page_context(
            request, texts=raw_texts or [""], selected_voice_id=request.POST.get("voice_id", ""),
            name_prefix=name_prefix,
        ), status=400)

    with transaction.atomic():
        batch = StudioBatch.objects.create(
            user=request.user,
            voice=voice,
            name_prefix=name_prefix,
            status=StudioBatch.Status.PENDING,
            total_items=len(texts),
            queued_at=timezone.now(),
            status_message="Lot ajouté à la file de génération.",
        )
        StudioBatchItem.objects.bulk_create([
            StudioBatchItem(
                batch=batch,
                position=index,
                text=text,
                asset_name=f"{name_prefix} {index + 1:02d}".strip() if name_prefix else f"Narration {index + 1:02d}",
            )
            for index, text in enumerate(texts)
        ])
    messages.success(request, "Le lot a été ajouté à la file de génération.")
    return redirect(f"{reverse('studio_home')}?batch={batch.pk}")


@login_required
@require_GET
def studio_batch_status(request, batch_id):
    batch = get_object_or_404(StudioBatch, pk=batch_id, user=request.user)
    items = []
    for item in batch.items.select_related("audio_asset").order_by("position", "id"):
        items.append({
            "position": item.position + 1,
            "name": item.asset_name,
            "status": item.status,
            "status_label": item.get_status_display(),
            "error": item.error_message,
            "audio_url": item.audio_asset.audio_file.url if item.audio_asset and item.audio_asset.audio_file else None,
        })
    return JsonResponse({
        "id": batch.pk,
        "status": batch.status,
        "status_label": batch.get_status_display(),
        "status_message": batch.status_message,
        "total_items": batch.total_items,
        "completed_items": batch.completed_items,
        "current_item": batch.current_item,
        "progress_percent": batch.progress_percent,
        "items": items,
    })


@login_required
def audios(request):
    per_page = get_per_page(request)
    assets, folder_context = _filter_media_folder(
        request, AudioAsset.objects.filter(user=request.user).select_related("voice").order_by("-created_at", "-id"),
    )
    audio_page, page_response = _page_or_redirect(
        request, assets, per_page,
    )
    if page_response:
        return page_response
    return _audio_library_response(request, audio_page=audio_page, folder_context=folder_context)


def _audio_library_response(request, *, status=200, audio_page=None, folder_context=None):
    if folder_context is None:
        assets, folder_context = _filter_media_folder(
            request, AudioAsset.objects.filter(user=request.user).select_related("voice").order_by("-created_at", "-id"),
        )
    else:
        assets = None
    if audio_page is None:
        audio_page = _get_page(
            assets,
            request.GET.get("page"), get_per_page(request),
        )
    audio_assets = audio_page.object_list
    for asset in audio_assets:
        minutes, seconds = divmod(max(0, round(asset.duration)), 60)
        asset.duration_mmss = f"{minutes:02d}:{seconds:02d}"
        voice = asset.voice
        asset.can_regenerate_text = bool(
            asset.source == AudioAsset.Source.XTTS
            and voice
            and voice.user_id == request.user.id
            and voice.consent_confirmed
            and voice.sample
            and voice.sample.name
            and voice.sample.storage.exists(voice.sample.name)
        )
    context = {
        "audio_assets": audio_assets,
        "audio_page": audio_page,
        "audio_per_page": audio_page.paginator.per_page,
        "pagination_params": _pagination_params(request, preserve=("folder",)),
        "projects": NarrationProject.objects.filter(user=request.user)[:30],
    }
    context.update(folder_context)
    return render(request, "studio/audios.html", context, status=status)


def _audio_text_edit_context(request, asset, *, text=None, error=""):
    voice = asset.voice
    can_regenerate = bool(
        asset.source == AudioAsset.Source.XTTS
        and voice
        and voice.user_id == request.user.id
        and voice.consent_confirmed
        and voice.sample
        and voice.sample.name
        and voice.sample.storage.exists(voice.sample.name)
    )
    return {
        "audio_asset": asset,
        "audio_text": asset.text if text is None else text,
        "can_regenerate": can_regenerate,
        "error_message": error,
        "max_text_chars": MAX_STUDIO_TEXT_CHARS,
    }


@login_required
@require_http_methods(["GET", "POST"])
def edit_audio_asset(request, asset_id):
    asset = get_object_or_404(
        AudioAsset.objects.select_related("voice"), pk=asset_id, user=request.user,
    )
    if request.method == "GET":
        return render(request, "studio/audio_asset_edit.html", _audio_text_edit_context(request, asset))

    def reject(message, status=400):
        if request.headers.get("x-requested-with") == "XMLHttpRequest":
            return JsonResponse({"success": False, "message": message}, status=status)
        return render(request, "studio/audio_asset_edit.html", _audio_text_edit_context(
            request, asset, text=request.POST.get("text", ""), error=message,
        ), status=status)

    if not _audio_text_edit_context(request, asset)["can_regenerate"]:
        return reject("Cet audio ne peut pas être régénéré : vérifie sa source et sa voix.", status=400)

    new_text = (request.POST.get("text") or "").strip()
    if not new_text:
        return reject("Le texte ne peut pas être vide.")
    if len(new_text) > MAX_STUDIO_TEXT_CHARS:
        return reject(f"Le texte ne peut pas dépasser {MAX_STUDIO_TEXT_CHARS} caractères.")
    if new_text == (asset.text or "").strip():
        message = "Texte identique, aucune régénération nécessaire."
        messages.info(request, message)
        if request.headers.get("x-requested-with") == "XMLHttpRequest":
            return JsonResponse({
                "success": True, "unchanged": True, "message": message,
                "redirect_url": reverse("edit_audio_asset", args=[asset.pk]),
            })
        return redirect("edit_audio_asset", asset_id=asset.pk)

    try:
        regenerate_audio_asset(asset, new_text)
    except Exception:
        logger.exception("Échec de régénération de l’audio %s pour l’utilisateur %s.", asset.pk, request.user.pk)
        return reject("La régénération a échoué. L’ancien audio est conservé.", status=500)

    message = "Audio régénéré avec succès."
    messages.success(request, message)
    if request.headers.get("x-requested-with") == "XMLHttpRequest":
        return JsonResponse({"success": True, "message": message, "redirect_url": reverse("audios")})
    return redirect("audios")


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
        if NarrationProject.objects.filter(audio_asset=asset).exists():
            messages.error(request, "Impossible de supprimer cet audio : il est référencé par un projet.")
            return _audio_library_response(request, status=400)
        audio_path = asset.audio_file.path if asset.audio_file else None
        try:
            asset.delete()
        except ProtectedError:
            messages.error(request, "Impossible de supprimer cet audio : il est référencé par un projet.")
            return _audio_library_response(request, status=400)
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
    if StudioBatch.objects.filter(
        voice=voice,
        status__in=[StudioBatch.Status.PENDING, StudioBatch.Status.PROCESSING],
    ).exists():
        messages.error(request, "Cette voix est utilisée par un lot Studio en attente ou en cours de traitement.")
        return redirect("home")
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
    terminal_batches = StudioBatch.objects.filter(
        user=request.user,
        status__in=[
            StudioBatch.Status.COMPLETED,
            StudioBatch.Status.PARTIAL,
            StudioBatch.Status.FAILED,
            StudioBatch.Status.CANCELLED,
        ],
    ).select_related("voice").prefetch_related("items__audio_asset").order_by("-finished_at", "-updated_at", "-id")
    return render(request, "studio/queue.html", {
        "processing_projects": processing_projects,
        "waiting_projects": waiting_projects,
        "terminal_batches": terminal_batches,
        "terminal_batch_count": terminal_batches.count(),
        "projects": NarrationProject.objects.filter(user=request.user)[:30],
    })


@login_required
@require_POST
def dismiss_completed_batches(request):
    terminal_batches = StudioBatch.objects.filter(
        user=request.user,
        status__in=[
            StudioBatch.Status.COMPLETED,
            StudioBatch.Status.PARTIAL,
            StudioBatch.Status.FAILED,
            StudioBatch.Status.CANCELLED,
        ],
    )
    batch_count = terminal_batches.count()
    if batch_count:
        terminal_batches.delete()
        messages.success(request, f"{batch_count} lot(s) Studio terminé(s) ont été effacés. Les audios générés sont conservés.")
    else:
        messages.info(request, "Aucun lot Studio terminé à effacer.")
    return redirect("queue_view")


def _queue_project_payload(project, position=None):
    return {
        "id": project.pk,
        "title": project.title,
        "status": project.status,
        "status_label": project.get_status_display(),
        "status_message": project.status_message,
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
    assembly_boundaries = []
    if project.audio_asset and isinstance(project.audio_asset.timings, dict):
        candidate_boundaries = project.audio_asset.timings.get("scene_boundaries")
        if isinstance(candidate_boundaries, list):
            assembly_boundaries = candidate_boundaries
    assembly_slot_count = max(1, min(len(assembly_boundaries) or 1, 30))
    segments_by_position = {}
    for segment in segments:
        if 0 <= segment.position < assembly_slot_count:
            segments_by_position.setdefault(segment.position, segment)
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
        assembly_animation = segment.assembly_animation_settings or default_assembly_animation_settings()
        segment.assembly_animation_settings_json = json.dumps(
            assembly_animation, ensure_ascii=False, separators=(",", ":"),
        )
        segment.assembly_animation_controls = []
        for definition in ANIMATION_EFFECT_DEFINITIONS:
            config = assembly_animation["effects"][definition["key"]]
            segment.assembly_animation_controls.append({
                **definition,
                "enabled": config["enabled"],
                "level": config["level"],
                "direction": config.get("direction", ""),
            })
    assembly_audio_assets = list(
        AudioAsset.objects.filter(
            user=request.user,
            source=AudioAsset.Source.XTTS,
            timings__isnull=False,
        ).order_by("-created_at", "-id")
    )
    for asset in assembly_audio_assets:
        try:
            asset.preview_url = asset.audio_file.url if asset.audio_file else ""
        except (OSError, ValueError):
            asset.preview_url = ""
    assembly_audio_asset_ids = {asset.pk for asset in assembly_audio_assets}
    return render(request, "studio/project_editor.html", {
        "project": project,
        "projects": NarrationProject.objects.filter(user=request.user)[:30],
        "segments": segments,
        "audio_ready": bool(segments and all(segment.ready_for_voice for segment in segments)),
        "voices": Voice.objects.filter(user=request.user),
        "background_tracks": BackgroundTrack.objects.filter(user=request.user),
        "assembly_audio_assets": assembly_audio_assets,
        "assembly_audio_unavailable": bool(
            project.audio_asset_id and project.audio_asset_id not in assembly_audio_asset_ids
        ),
        "assembly_slot_count": assembly_slot_count,
        "assembly_slots": [
            {"position": position, "segment": segments_by_position.get(position)}
            for position in range(assembly_slot_count)
        ],
        "project_modes": NarrationProject.Mode.choices,
        "categories": Voice.Category.choices,
        "video_formats": NarrationProject.VideoFormat.choices,
        "motions": NarrationSegment.Motion.choices,
        "assembly_motions": NarrationSegment.AssemblyMotion.choices,
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
def set_project_mode(request, project_id):
    project = get_object_or_404(NarrationProject, pk=project_id, user=request.user)
    if project.status in {NarrationProject.Status.QUEUED, NarrationProject.Status.PROCESSING}:
        return JsonResponse({"ok": False, "error": "Attends la fin du traitement avant de changer de mode."}, status=409)
    mode = request.POST.get("mode")
    if mode not in NarrationProject.Mode.values:
        return HttpResponseBadRequest("Mode de projet invalide.")
    if project.mode != mode:
        project.mode = mode
        _invalidate_video(project, extra_update_fields=["mode"])
        messages.success(request, "Le mode du projet a été enregistré. Les caches XTTS sont conservés.")
    return redirect("project_detail", project_id=project.pk)


@login_required
@require_POST
def set_project_audio_asset(request, project_id):
    project = get_object_or_404(NarrationProject, pk=project_id, user=request.user)
    if project.status in {NarrationProject.Status.QUEUED, NarrationProject.Status.PROCESSING}:
        return JsonResponse({"ok": False, "error": "Attends la fin du traitement avant de changer l’audio."}, status=409)
    raw_asset_id = (request.POST.get("audio_asset_id") or "").strip()
    audio_asset = None
    if raw_asset_id:
        try:
            raw_asset_id = int(raw_asset_id)
        except (TypeError, ValueError):
            raise Http404
        audio_asset = get_object_or_404(
            AudioAsset, pk=raw_asset_id, user=request.user, source=AudioAsset.Source.XTTS,
        )
    if project.audio_asset_id != (audio_asset.pk if audio_asset else None):
        project.audio_asset = audio_asset
        _invalidate_video(project, extra_update_fields=["audio_asset"])
        messages.success(request, "L’audio du projet a été enregistré. La narration XTTS en cache reste intacte.")
    return redirect("project_detail", project_id=project.pk)


@login_required
@require_POST
def save_assembly_visual(request, project_id):
    project = get_object_or_404(NarrationProject, pk=project_id, user=request.user)
    if project.mode != NarrationProject.Mode.ASSEMBLY:
        return HttpResponseBadRequest("Les visuels d’assemblage ne sont disponibles que dans ce mode.")
    if project.status in {NarrationProject.Status.QUEUED, NarrationProject.Status.PROCESSING}:
        return JsonResponse({"ok": False, "error": "Attends la fin du traitement avant de modifier un visuel."}, status=409)
    try:
        position = int(request.POST.get("position", ""))
    except (TypeError, ValueError):
        return HttpResponseBadRequest("Emplacement visuel invalide.")
    if position < 0 or position >= 30:
        return HttpResponseBadRequest("Emplacement visuel invalide.")
    if request.FILES.get("image") or request.FILES.get("video") or request.FILES.get("video_clip"):
        if request.FILES.get("image") and (request.FILES.get("video") or request.FILES.get("video_clip")):
            return HttpResponseBadRequest("Choisis une image ou une vidéo, pas les deux.")
        return HttpResponseBadRequest("Utilise le champ visuel unique du formulaire.")
    uploads = request.FILES.getlist("visual_file")
    if len(uploads) != 1:
        return HttpResponseBadRequest("Sélectionne une image ou une vidéo.")
    upload = uploads[0]
    visual_type = request.POST.get("visual_type")
    suffix = Path(upload.name).suffix.lower()
    if visual_type == "image" and suffix not in {".jpg", ".jpeg", ".png"}:
        return HttpResponseBadRequest("Formats image acceptés : JPG et PNG.")
    if visual_type == "video" and suffix not in {".mp4", ".mov"}:
        return HttpResponseBadRequest("Formats vidéo acceptés : MP4 et MOV.")
    if visual_type not in {"image", "video"}:
        return HttpResponseBadRequest("Choisis le type Image ou Vidéo.")
    if upload.size <= 0:
        return HttpResponseBadRequest("Le fichier sélectionné est vide.")
    if visual_type == "video" and upload.size > MAX_VIDEO_CLIP_BYTES:
        return HttpResponseBadRequest("La vidéo dépasse la limite de 200 Mo.")
    if visual_type == "image" and upload.size > 25 * 1024 * 1024:
        return HttpResponseBadRequest("L’image dépasse la limite de 25 Mo.")

    created_files = []
    try:
        with tempfile.TemporaryDirectory(prefix=f"petit-studio-assembly-image-{project.pk}-") as folder:
            staging_dir = Path(folder)
            if visual_type == "image":
                staged_scenes, _pixels = _scene_images(upload, staging_dir, max_scenes=1)
                if len(staged_scenes) != 1:
                    raise ValueError("Sélectionne une seule image JPG ou PNG pour cet emplacement.")
                filename, staged_path = staged_scenes[0]
            else:
                filename = Path(upload.name).name
                staged_path = staging_dir / f"source{suffix}"
                _stream_upload_to_path(upload, staged_path)
                try:
                    _probe_clip(staged_path)
                except RuntimeError as exc:
                    raise ValueError(f"La vidéo est illisible : {exc}") from exc

            segment = NarrationSegment.objects.filter(project=project, position=position).order_by("id").first()
            if segment is None:
                segment = NarrationSegment(project=project, position=position)
                is_new = True
            else:
                is_new = False
            old_files = []
            if segment.image and segment.image.name:
                old_files.append((segment.image.storage, segment.image.name))
            if segment.video_clip and segment.video_clip.name:
                old_files.append((segment.video_clip.storage, segment.video_clip.name))
            if visual_type == "image":
                segment.video_clip = ""
                _save_staged_file(segment.image, filename, staged_path, created_files)
            else:
                segment.image = ""
                _save_staged_file(segment.video_clip, filename, staged_path, created_files)
            with transaction.atomic():
                if is_new:
                    segment.save()
                else:
                    segment.save(update_fields=["image", "video_clip", "updated_at"])
                _invalidate_video(project)
                _schedule_storage_cleanup(old_files)
        label = "L’image" if visual_type == "image" else "La vidéo"
        messages.success(request, f"{label} de l’emplacement {position + 1} a été enregistrée.")
    except ValueError as exc:
        _delete_storage_files(created_files)
        messages.error(request, str(exc))
    except Exception:
        _delete_storage_files(created_files)
        logger.exception("Échec de l’enregistrement d’un visuel d’assemblage pour le projet %s.", project.pk)
        messages.error(request, "Impossible d’enregistrer ce visuel.")
    return redirect("project_detail", project_id=project.pk)


@login_required
@require_POST
def save_assembly_motion(request, project_id, segment_id):
    project = get_object_or_404(
        NarrationProject, pk=project_id, user=request.user, mode=NarrationProject.Mode.ASSEMBLY,
    )
    segment = get_object_or_404(NarrationSegment, pk=segment_id, project=project)
    if project.status in {NarrationProject.Status.QUEUED, NarrationProject.Status.PROCESSING}:
        return HttpResponse("Attends la fin du traitement avant de modifier un effet.", status=409)
    if not segment.image or not segment.image.name or (segment.video_clip and segment.video_clip.name):
        return HttpResponseBadRequest("Les effets de mouvement sont disponibles uniquement pour une image fixe.")

    assembly_motion = request.POST.get("assembly_motion", "")
    if assembly_motion not in NarrationSegment.AssemblyMotion.values:
        return HttpResponseBadRequest("Choisis un effet de mouvement valide.")

    settings = _animation_settings_for_assembly_motion(assembly_motion)
    _save_assembly_animation(project, segment, settings)
    return redirect("project_detail", project_id=project.pk)


def _save_assembly_animation(project, segment, settings):
    legacy_motion = _assembly_motion_from_animation(settings)
    changed = (
        settings != (segment.assembly_animation_settings or default_assembly_animation_settings())
        or segment.assembly_motion != legacy_motion
    )
    if changed:
        segment.assembly_animation_settings = settings
        segment.assembly_motion = legacy_motion
        segment.save(update_fields=["assembly_animation_settings", "assembly_motion", "updated_at"])
        _invalidate_video(project)
    if settings["effects"]["parallax"]["enabled"]:
        messages.warning(request, "Effet bientôt disponible en Assemblage : la parallaxe reste sans effet dans ce mode.")
    elif changed:
        messages.success(request, "Les effets d’image ont été enregistrés.")
    else:
        messages.info(request, "Aucun changement à enregistrer.")


@login_required
@require_POST
def save_assembly_animation(request, project_id, segment_id):
    project = get_object_or_404(
        NarrationProject, pk=project_id, user=request.user, mode=NarrationProject.Mode.ASSEMBLY,
    )
    segment = get_object_or_404(NarrationSegment, pk=segment_id, project=project)
    if project.status in {NarrationProject.Status.QUEUED, NarrationProject.Status.PROCESSING}:
        return HttpResponse("Attends la fin du traitement avant de modifier les effets.", status=409)
    if not segment.image or not segment.image.name or (segment.video_clip and segment.video_clip.name):
        return HttpResponseBadRequest("Les effets sont disponibles uniquement pour une image fixe.")
    try:
        settings = _validate_animation_settings_json(request.POST.get("animation_settings"))
    except ValueError as exc:
        return HttpResponseBadRequest(str(exc))
    _save_assembly_animation(project, segment, settings)
    return redirect("project_detail", project_id=project.pk)


@login_required
@require_POST
def render_assembly(request, project_id):
    """Queue a direct FFmpeg render from a user-owned AudioAsset and visual slots."""
    project = get_object_or_404(NarrationProject, pk=project_id, user=request.user)
    if project.mode != NarrationProject.Mode.ASSEMBLY:
        return JsonResponse({"ok": False, "error": "Passe le projet en mode Assemblage avant de lancer le rendu."}, status=400)
    if project.status in {NarrationProject.Status.QUEUED, NarrationProject.Status.PROCESSING}:
        return JsonResponse({"ok": False, "error": "Ce projet est déjà dans la file ou en cours de traitement."}, status=409)
    if project.audio_asset_id and project.audio_asset.user_id != request.user.id:
        raise Http404
    try:
        inputs = validate_assembly_project(project)
    except ValueError as exc:
        return JsonResponse({"ok": False, "error": str(exc)}, status=400)

    now = timezone.now()
    project.status = NarrationProject.Status.QUEUED
    project.queued_at = now
    project.job_type = NarrationProject.JobType.AUDIO_ASSEMBLY
    project.cancel_requested = False
    project.status_message = "Rendu Assemblage ajouté à la file d’attente."
    project.error_message = ""
    project.progress_percent = 0
    project.current_segment = 0
    project.total_segments = len(inputs["boundaries"])
    project.started_at = None
    project.finished_at = None
    project.save(update_fields=[
        "status", "queued_at", "job_type", "cancel_requested", "status_message", "error_message",
        "progress_percent", "current_segment", "total_segments", "started_at", "finished_at", "updated_at",
    ])
    return JsonResponse({"ok": True, "status_url": f"/projects/{project.pk}/status/"})


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
    if project.mode != NarrationProject.Mode.SCENES:
        return JsonResponse({"ok": False, "error": "L’assemblage de clips est réservé au mode Scènes."}, status=400)
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
    with transaction.atomic():
        files = [
            (field.storage, field.name)
            for field in (segment.audio, segment.image, segment.video_clip)
            if field and field.name
        ]
        segment.delete()
        for index, item in enumerate(project.segments.order_by("position", "id")):
            if item.position != index:
                NarrationSegment.objects.filter(pk=item.pk).update(position=index)
        _invalidate_export(project)
        _schedule_storage_cleanup(files)
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
        if request.POST.get("confirm") != "1":
            return HttpResponseBadRequest("Confirmation manquante")
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
    fallback_url = reverse("home")
    return_to = _safe_return_url(request, fallback_url)
    if return_to.split("?", 1)[0] == reverse("project_detail", args=[project_id]):
        return_to = fallback_url
    return redirect(return_to)


def _safe_return_url(request, fallback_url):
    candidate = (request.POST.get("next") or "").strip()
    if candidate and url_has_allowed_host_and_scheme(
        candidate,
        allowed_hosts={request.get_host()},
        require_https=request.is_secure(),
    ):
        return candidate
    return fallback_url


def _duplicate_project_title(user, title):
    copy_number = 1
    while True:
        suffix = " (copie)" if copy_number == 1 else f" (copie {copy_number})"
        candidate = f"{title[:120 - len(suffix)].rstrip()}{suffix}"
        if not NarrationProject.objects.filter(user=user, title=candidate).exists():
            return candidate
        copy_number += 1


@login_required
@require_POST
def duplicate_project(request, project_id):
    with transaction.atomic():
        source = get_object_or_404(
            NarrationProject.objects.select_for_update(), pk=project_id, user=request.user,
        )
        if source.status in {NarrationProject.Status.QUEUED, NarrationProject.Status.PROCESSING}:
            messages.error(request, "Attends la fin du traitement avant de dupliquer ce projet.")
            return redirect("project_detail", project_id=source.pk)

        duplicate = NarrationProject.objects.create(
            user=request.user,
            folder=source.folder,
            title=_duplicate_project_title(request.user, source.title),
            mode=source.mode,
            voice=source.voice,
            audio_asset=source.audio_asset,
            background_track=source.background_track,
            music_volume=source.music_volume,
            video_format=source.video_format,
            subtitles_enabled=source.subtitles_enabled,
            status=NarrationProject.Status.DRAFT,
            queued_at=None,
            cancel_requested=False,
            job_type=NarrationProject.JobType.AUDIO,
            status_message="",
            error_message="",
            progress_percent=0,
            current_segment=0,
            total_segments=0,
            started_at=None,
            finished_at=None,
            full_audio="",
            subtitles="",
            video_file="",
            duration_seconds=0,
        )

        for segment in source.segments.order_by("position", "id"):
            NarrationSegment.objects.create(
                project=duplicate,
                position=segment.position,
                image=segment.image.name if segment.image else "",
                video_clip=segment.video_clip.name if segment.video_clip else "",
                assembly_motion=segment.assembly_motion,
                assembly_animation_settings=deepcopy(segment.assembly_animation_settings),
                motion=segment.motion,
                camera_profile=segment.camera_profile,
                light_breathing=segment.light_breathing,
                animation_settings=deepcopy(segment.animation_settings),
                text=segment.text,
                audio="",
                subtitle_data=[],
                duration_seconds=0,
                cache_key="",
            )

    messages.success(request, "Projet dupliqué. Tu peux maintenant modifier les visuels.")
    return redirect("project_detail", project_id=duplicate.pk)


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
    title = (request.POST.get("title") or "").strip()
    if not title:
        messages.error(request, "Le nom ne peut pas être vide.")
    elif len(title) > 120:
        messages.error(request, "Le nom est trop long (max 120 caractères).")
    else:
        if title != project.title:
            project.title = title
            project.save(update_fields=["title", "updated_at"])
        messages.success(request, "Projet renommé.")
    return_to = _safe_return_url(request, reverse("project_detail", args=[project.pk]))
    return redirect(return_to)


@login_required
@require_POST
def prepare(request, project_id):
    project = get_object_or_404(NarrationProject, pk=project_id, user=request.user)
    if project.mode == NarrationProject.Mode.ASSEMBLY:
        return JsonResponse({"ok": False, "error": "Utilise le bouton Rendre la vidéo du mode Assemblage."}, status=400)
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
