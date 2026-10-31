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
        PROCESSING = "processing", "En cours"
        READY = "ready", "Prêt"
        ERROR = "error", "Erreur"

    title = models.CharField("titre", max_length=120, default="Nouvelle narration")
    voice = models.ForeignKey(Voice, null=True, blank=True, on_delete=models.SET_NULL, related_name="projects")
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.DRAFT)
    status_message = models.CharField(max_length=240, blank=True)
    error_message = models.TextField(blank=True)
    progress_percent = models.PositiveSmallIntegerField(default=0)
    current_segment = models.PositiveSmallIntegerField(default=0)
    total_segments = models.PositiveSmallIntegerField(default=0)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    full_audio = models.FileField(upload_to="exports/audio/", blank=True)
    subtitles = models.FileField(upload_to="exports/subtitles/", blank=True)
    duration_seconds = models.FloatField(default=0)
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-updated_at", "-id"]

    def __str__(self):
        return self.title


class NarrationSegment(models.Model):
    project = models.ForeignKey(NarrationProject, on_delete=models.CASCADE, related_name="segments")
    position = models.PositiveSmallIntegerField(default=0)
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
