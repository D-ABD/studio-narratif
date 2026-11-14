from .models import NarrationProject


def queue_count(request):
    """Expose the number of queued projects to every template using the base layout."""
    return {
        "queue_count": NarrationProject.objects.filter(
            status=NarrationProject.Status.QUEUED,
        ).count(),
    }
