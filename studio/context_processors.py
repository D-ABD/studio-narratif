from django.db.models import Count

from .models import MediaFolder, NarrationProject, ProjectFolder


def queue_count(request):
    """Expose the authenticated user's queue count and recent projects."""
    if not request.user.is_authenticated:
        return {
            "queue_count": 0, "sidebar_projects": (), "sidebar_project_count": 0,
            "sidebar_folders": (), "sidebar_media_folders": (),
        }
    project_queryset = NarrationProject.objects.filter(user=request.user).order_by("-updated_at", "-id")
    return {
        "queue_count": NarrationProject.objects.filter(
            user=request.user,
            status=NarrationProject.Status.QUEUED,
        ).count(),
        "sidebar_projects": project_queryset[:15],
        "sidebar_project_count": project_queryset.count(),
        "sidebar_root_project_count": NarrationProject.objects.filter(user=request.user, folder__isnull=True).count(),
        "sidebar_folders": ProjectFolder.objects.filter(user=request.user)
        .annotate(project_count=Count("projects"))
        .order_by("name", "id"),
        "sidebar_media_folders": MediaFolder.objects.filter(user=request.user)
        .annotate(
            audio_count=Count("audio_assets", distinct=True),
            music_count=Count("background_tracks", distinct=True),
        )
        .order_by("name", "id"),
    }
