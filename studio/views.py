import json
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
    ANIMATION_EFFECTS, ANIMATION_LEVEL_CHOICES, AudioAsset, BackgroundTrack, NarrationProject,
    NarrationSegment, StudioBatch, StudioBatchItem, Voice,
    default_animation_settings, legacy_animation_settings,
)
from .services import _voice_sample_digest, cache_key_for, hash_file, normalize_voice_sample, srt_text
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
MAX_STUDIO_BATCH_ITEMS = 20
MAX_STUDIO_TEXT_CHARS = 12000
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
    if type(settings["versio