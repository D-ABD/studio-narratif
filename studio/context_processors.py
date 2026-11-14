from .models import NarrationProject


def queue_count(request):
    """Expose only the authenticated user's queued-project count."""
    if not request.user.is_authenticated:
        return {"queue_count": 0}
    return {
        "queue_count": NarrationProject.objects.filter(
            user=request.user,
            status=NarrationProject.Status.QUEUED,
        ).count(),
    }
