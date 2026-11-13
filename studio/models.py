from django.db import models
from django.utils import timezone


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


class NarrationProject(models.Model):
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
        VIDEO_NARRATION = "video_narration", "Narration vidéo"

    title = models.CharField("titre", max_length=120, default="Nouvelle narration")
    voice = models.ForeignKey(Voice, null=True, blank=True, on_delete=models.SET_NULL, related_name="projects")
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.DRAFT)
    queued_at = models.DateTimeField(null=True, blank=True, db_index=True)
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

    def __str__(self):
        return self.title


class NarrationSegment(models.Model):
    class Motion(models.TextChoices):
        STILL = "still", "Image fixe"
        ZOOM_IN = "zoom_in", "Zoom avant lent"
        ZOOM_OUT = "zoom_out", "Zoom arrière lent"
        PAN_H = "pan_h", "Panoramique gauche → droite"
        PAN_V = "pan_v", "Panoramique haut → bas"
        ZOOM_PAN = "zoom_pan", "Zoom diagonal doux"
        PARALLAX = "parallax", "Profondeur douce · parallaxe 2.5D"

    class CameraProfile(models.TextChoices):
        LINEAR = "linear", "Linéaire"
        ORGANIC = "organic", "Organique"

    project = models.ForeignKey(NarrationProject, on_delete=models.CASCADE, related_name="segments")
    position = models.PositiveSmallIntegerField(default=0)
    image = models.ImageField(upload_to="scenes/", blank=True)
    video_clip = models.FileField(upload_to="scenes/videos/", blank=True)
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
