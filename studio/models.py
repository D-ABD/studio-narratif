from django.conf import settings
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.db.models import Q
from django.utils import timezone
from pathlib import Path
import uuid


ANIMATION_LEVEL_CHOICES = (
    ("slow", "Lent"),
    ("medium", "Moyen"),
    ("fast", "Rapide"),
)
ANIMATION_EFFECTS = ("zoom", "pan", "parallax", "handheld", "light_breathing", "light_leak")


def default_animation_settings():
    return {
        "version": 1,
        "camera_profile": "linear",
        "effects": {
            "zoom": {"enabled": True, "level": "medium", "direction": "in"},
            "pan": {"enabled": False, "level": "medium", "direction": "horizontal"},
            "parallax": {"enabled": False, "level": "medium"},
            "handheld": {"enabled": False, "level": "medium"},
            "light_breathing": {"enabled": False, "level": "medium"},
            "light_leak": {"enabled": False, "level": "medium"},
        },
    }


def default_assembly_animation_settings():
    """Assembly keeps its historic still-image default while allowing a full effect stack."""
    settings = default_animation_settings()
    for effect in settings["effects"].values():
        effect["enabled"] = False
    return settings


def legacy_animation_settings(motion, camera_profile="linear", light_breathing=False):
    """Translate the former single-motion fields into the multi-effect schema."""
    settings = default_animation_settings()
    effects = settings["effects"]
    for effect in effects.values():
        effect["enabled"] = False

    if motion == "zoom_in":
        effects["zoom"].update(enabled=True, direction="in")
    elif motion == "zoom_out":
        effects["zoom"].update(enabled=True, direction="out")
    elif motion == "pan_h":
        effects["pan"].update(enabled=True, direction="horizontal")
    elif motion == "pan_v":
        effects["pan"].update(enabled=True, direction="vertical")
    elif motion == "zoom_pan":
        effects["zoom"].update(enabled=True, direction="in")
        effects["pan"].update(enabled=True, direction="diagonal")
    elif motion == "parallax":
        effects["parallax"]["enabled"] = True

    if camera_profile in {"linear", "organic"}:
        settings["camera_profile"] = camera_profile
    effects["light_breathing"]["enabled"] = bool(light_breathing)
    return settings


class Voice(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="voices")
    class Category(models.TextChoices):
        SHIBA = "shiba", "Shiba"
        MAN = "homme", "Homme"
        WOMAN = "femme", "Femme"
        OTHER = "autre", "Autre"

    name = models.CharField("nom", max_length=80)
    category = models.CharField("catégorie", max_length=12, choices=Category.choices, default=Category.OTHER)
    sample = models.FileField("échantillon vocal", upload_to="voices/")
    sample_sha256 = models.CharField(max_length=64, blank=True)
    consent_confirmed = models.BooleanField("autorisation confirmée", default=False)
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name", "id"]

    def __str__(self):
        return self.name


def background_track_upload_to(instance, filename):
    extension = Path(filename).suffix.lower()
    return f"musics/user_{instance.user_id}/{uuid.uuid4().hex}{extension}"


def audio_asset_upload_to(instance, filename):
    extension = Path(filename).suffix.lower()
    return f"audios/user_{instance.user_id}/{uuid.uuid4().hex}{extension}"


def audio_session_upload_to(instance, filename):
    extension = Path(filename).suffix.lower()
    return f"audio-sessions/user_{instance.user_id}/{uuid.uuid4().hex}{extension}"


def video_merge_upload_to(instance, filename):
    return f"exports/merge/user_{instance.user_id}/{uuid.uuid4().hex}.mp4"


class MediaFolder(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="media_folders")
    name = models.CharField("nom", max_length=120)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["name", "id"]

    def __str__(self):
        return self.name


class BackgroundTrack(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="background_tracks")
    folder = models.ForeignKey(
        MediaFolder, null=True, blank=True, on_delete=models.SET_NULL, related_name="background_tracks",
    )
    name = models.CharField("nom", max_length=120)
    audio_file = models.FileField("fichier audio", upload_to=background_track_upload_to)
    duration = models.FloatField("durée en secondes", validators=[MinValueValidator(0)])
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name", "id"]

    def __str__(self):
        return self.name


class AudioAsset(models.Model):
    class Source(models.TextChoices):
        UPLOAD = "upload", "Importé"
        XTTS = "xtts", "Généré par XTTS"

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="audio_assets")
    folder = models.ForeignKey(
        MediaFolder, null=True, blank=True, on_delete=models.SET_NULL, related_name="audio_assets",
    )
    name = models.CharField("nom", max_length=160)
    audio_file = models.FileField("fichier audio", upload_to=audio_asset_upload_to)
    duration = models.FloatField("durée en secondes", validators=[MinValueValidator(0)])
    # NULL in this CRUD lot; filled by the standalone studio in phase 3.7.
    # Shape: {"version": 1, "words": [{"word": str, "start": float, "end": float}],
    # "segments": [{"text": str, "start": float, "end": float}],
    # "scene_boundaries": [{"scene_index": int, "start": float, "end": float}]}.
    # Mono-scene uses [{"scene_index": 0, "start": 0.0, "end": duration}];
    # multi-scene stores N boundaries so phase 3.8 can use the same assembly path.
    timings = models.JSONField("timings", null=True, blank=True)
    source = models.CharField(max_length=8, choices=Source.choices, default=Source.UPLOAD)
    text = models.TextField(null=True, blank=True)
    voice = models.ForeignKey(
        Voice, null=True, blank=True, on_delete=models.SET_NULL, related_name="audio_assets",
    )
    project = models.ForeignKey(
        "NarrationProject", null=True, blank=True, on_delete=models.SET_NULL, related_name="audio_assets",
    )
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at", "-id"]

    def __str__(self):
        return self.name


class AudioSession(models.Model):
    class SourceKind(models.TextChoices):
        UPLOAD = "upload", "Importé"
        FROM_MUSIC = "from_music", "Bibliothèque musicale"
        FROM_AUDIO = "from_audio", "Bibliothèque audio"

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="audio_sessions",
    )
    source_file = models.FileField("source audio", upload_to=audio_session_upload_to)
    source_kind = models.CharField(max_length=12, choices=SourceKind.choices)
    operations = models.JSONField(default=list, blank=True)
    current_preview_file = models.FileField(
        "aperçu audio", upload_to=audio_session_upload_to, blank=True,
    )
    # Versioned pad performance: {"version": 1, "recording_duration_ms": int,
    # "bpm": int, "events": [{"pad_index": int, "timestamp_ms": int}, ...]}.
    sequencer_data = models.JSONField("séquence des pads", null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-updated_at", "-id"]

    def __str__(self):
        return f"Session audio #{self.pk} · {self.user}"


class StudioBatch(models.Model):
    class Status(models.TextChoices):
        DRAFT = "draft", "Brouillon"
        PENDING = "pending", "En attente"
        PROCESSING = "processing", "En cours"
        COMPLETED = "completed", "Terminé"
        PARTIAL = "partial", "Terminé avec erreurs"
        FAILED = "failed", "Échec"
        CANCELLED = "cancelled", "Annulé"

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="studio_batches")
    voice = models.ForeignKey(Voice, null=True, blank=True, on_delete=models.SET_NULL, related_name="studio_batches")
    name_prefix = models.CharField(max_length=100, blank=True)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.DRAFT, db_index=True)
    total_items = models.PositiveSmallIntegerField(default=0)
    completed_items = models.PositiveSmallIntegerField(default=0)
    current_item = models.PositiveSmallIntegerField(default=0)
    progress_percent = models.PositiveSmallIntegerField(default=0)
    status_message = models.CharField(max_length=240, blank=True)
    error_message = models.TextField(blank=True)
    queued_at = models.DateTimeField(null=True, blank=True, db_index=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at", "-id"]

    def __str__(self):
        return f"Lot de narration #{self.pk} · {self.user}"


class StudioBatchItem(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "En attente"
        PROCESSING = "processing", "En cours"
        COMPLETED = "completed", "Terminé"
        FAILED = "failed", "Échec"
        CANCELLED = "cancelled", "Annulé"

    batch = models.ForeignKey(StudioBatch, on_delete=models.CASCADE, related_name="items")
    position = models.PositiveSmallIntegerField()
    text = models.TextField()
    asset_name = models.CharField(max_length=160)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.PENDING, db_index=True)
    audio_asset = models.ForeignKey(
        AudioAsset, null=True, blank=True, on_delete=models.SET_NULL, related_name="studio_batch_items",
    )
    error_message = models.TextField(blank=True)
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["position", "id"]
        constraints = [
            models.UniqueConstraint(fields=["batch", "position"], name="studio_batch_item_position_unique"),
        ]

    def __str__(self):
        return f"{self.batch} · narration {self.position + 1}"


class ProjectFolder(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="project_folders")
    name = models.CharField("nom", max_length=120)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["name", "id"]

    def __str__(self):
        return self.name


class NarrationProject(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="narration_projects")
    folder = models.ForeignKey(
        ProjectFolder, null=True, blank=True, on_delete=models.SET_NULL, related_name="projects",
    )
    class Status(models.TextChoices):
        DRAFT = "draft", "Brouillon"
        QUEUED = "queued", "En attente"
        PROCESSING = "processing", "En cours"
        READY = "ready", "Prêt"
        ERROR = "error", "Erreur"

    class VideoFormat(models.TextChoices):
        VERTICAL = "9:16", "Vertical · 9:16"
        HORIZONTAL = "16:9", "Horizontal · 16:9"

    class JobType(models.TextChoices):
        AUDIO = "audio", "Narration audio"
        VIDEO = "video", "Vidéo"
        ASSEMBLY = "assembly", "Assemblage de clips"
        AUDIO_ASSEMBLY = "audio_assembly", "Assemblage avec audio de la bibliothèque"
        VIDEO_NARRATION = "video_narration", "Narration vidéo"

    class Mode(models.TextChoices):
        SCENES = "scenes", "Scènes"
        ASSEMBLY = "assembly", "Assemblage"

    title = models.CharField("titre", max_length=120, default="Nouvelle narration")
    mode = models.CharField(max_length=12, choices=Mode.choices, default=Mode.SCENES, db_index=True)
    voice = models.ForeignKey(Voice, null=True, blank=True, on_delete=models.SET_NULL, related_name="projects")
    audio_asset = models.ForeignKey(
        AudioAsset, null=True, blank=True, on_delete=models.PROTECT, related_name="assembly_projects",
    )
    background_track = models.ForeignKey(
        BackgroundTrack, null=True, blank=True, on_delete=models.SET_NULL, related_name="projects",
    )
    music_volume = models.IntegerField(
        default=20, validators=[MinValueValidator(0), MaxValueValidator(100)],
    )
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.DRAFT)
    queued_at = models.DateTimeField(null=True, blank=True, db_index=True)
    cancel_requested = models.BooleanField(default=False)
    job_type = models.CharField(max_length=16, choices=JobType.choices, default=JobType.AUDIO)
    video_format = models.CharField(max_length=5, choices=VideoFormat.choices, default=VideoFormat.VERTICAL)
    subtitles_enabled = models.BooleanField(default=True)
    status_message = models.CharField(max_length=240, blank=True)
    error_message = models.TextField(blank=True)
    progress_percent = models.PositiveSmallIntegerField(default=0)
    current_segment = models.PositiveSmallIntegerField(default=0)
    total_segments = models.PositiveSmallIntegerField(default=0)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    full_audio = models.FileField(upload_to="exports/audio/", blank=True)
    subtitles = models.FileField(upload_to="exports/subtitles/", blank=True)
    video_file = models.FileField(upload_to="exports/video/", blank=True)
    duration_seconds = models.FloatField(default=0)
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-updated_at", "-id"]
        constraints = [
            models.CheckConstraint(
                condition=Q(music_volume__gte=0) & Q(music_volume__lte=100),
                name="project_music_volume_0_100",
            ),
        ]

    def __str__(self):
        return self.title


class VideoMerge(models.Model):
    class Status(models.TextChoices):
        QUEUED = "queued", "En attente"
        PROCESSING = "processing", "En cours"
        READY = "ready", "Prêt"
        ERROR = "error", "Erreur"

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="video_merges")
    source_projects = models.ManyToManyField(
        NarrationProject, through="VideoMergeItem", related_name="video_merges",
    )
    output_file = models.FileField(upload_to=video_merge_upload_to, blank=True)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.QUEUED, db_index=True)
    requires_reencode = models.BooleanField(default=False)
    duration_seconds = models.FloatField(default=0)
    progress_percent = models.PositiveSmallIntegerField(default=0)
    status_message = models.CharField(max_length=240, blank=True)
    error_message = models.TextField(blank=True)
    queued_at = models.DateTimeField(null=True, blank=True, default=timezone.now, db_index=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at", "-id"]

    def __str__(self):
        return f"Assemblage vidéo #{self.pk} · {self.user}"


class VideoMergeItem(models.Model):
    merge = models.ForeignKey(VideoMerge, on_delete=models.CASCADE, related_name="items")
    source_project = models.ForeignKey(
        NarrationProject, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="video_merge_items",
    )
    source_title = models.CharField(max_length=120)
    source_video_name = models.CharField(max_length=255)
    source_video_size = models.PositiveBigIntegerField(default=0)
    source_video_mtime_ns = models.PositiveBigIntegerField(default=0)
    source_metadata = models.JSONField(default=dict, blank=True)
    position = models.PositiveSmallIntegerField()

    class Meta:
        ordering = ["position", "id"]
        constraints = [
            models.UniqueConstraint(fields=["merge", "position"], name="unique_video_merge_item_position"),
        ]

    def __str__(self):
        return f"{self.source_title} · {self.position + 1}"


class NarrationSegment(models.Model):
    class Motion(models.TextChoices):
        STILL = "still", "Image fixe"
        ZOOM_IN = "zoom_in", "Zoom avant lent"
        ZOOM_OUT = "zoom_out", "Zoom arrière lent"
        PAN_H = "pan_h", "Panoramique gauche → droite"
        PAN_V = "pan_v", "Panoramique haut → bas"
        ZOOM_PAN = "zoom_pan", "Zoom diagonal doux"
        PARALLAX = "parallax", "Profondeur douce · parallaxe 2.5D"

    class AssemblyMotion(models.TextChoices):
        NONE = "none", "Aucun"
        ZOOM_IN = "zoom_in", "Zoom avant"
        ZOOM_OUT = "zoom_out", "Zoom arrière"
        PAN_LEFT = "pan_left", "Panoramique gauche"
        PAN_RIGHT = "pan_right", "Panoramique droit"
        PARALLAX = "parallax", "Parallaxe"

    class CameraProfile(models.TextChoices):
        LINEAR = "linear", "Linéaire"
        ORGANIC = "organic", "Organique"

    project = models.ForeignKey(NarrationProject, on_delete=models.CASCADE, related_name="segments")
    position = models.PositiveSmallIntegerField(default=0)
    image = models.ImageField(upload_to="scenes/", blank=True)
    video_clip = models.FileField(upload_to="scenes/videos/", blank=True)
    assembly_motion = models.CharField(
        max_length=12, choices=AssemblyMotion.choices, default=AssemblyMotion.NONE,
    )
    assembly_animation_settings = models.JSONField(
        default=default_assembly_animation_settings, blank=True,
    )
    motion = models.CharField(max_length=8, choices=Motion.choices, default=Motion.ZOOM_IN)
    camera_profile = models.CharField(
        max_length=8, choices=CameraProfile.choices, default=CameraProfile.LINEAR,
    )
    light_breathing = models.BooleanField(default=False)
    animation_settings = models.JSONField(default=default_animation_settings, blank=True)
    text = models.TextField(blank=True)
    audio = models.FileField(upload_to="segments/audio/", blank=True)
    subtitle_data = models.JSONField(default=list, blank=True)
    duration_seconds = models.FloatField(default=0)
    cache_key = models.CharField(max_length=64, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["position", "id"]

    def __str__(self):
        return f"{self.project.title} · segment {self.position + 1}"
