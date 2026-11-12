from django.db import models
from django.utils import timezone


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

    title = models.CharField("titre", max_length=120, default="Nouvelle narration")
    voice = models.ForeignKey(Voice, null=True, blank=True, on_delete=models.SET_NULL, related_name="projects")
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.DRAFT)
    queued_at = models.DateTimeField(null=True, blank=True, db_index=True)
    job_type = models.CharField(max_length=8, choices=JobType.choices, default=JobType.AUDIO)
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
