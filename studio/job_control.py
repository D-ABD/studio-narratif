import time

from django.utils import timezone

from .models import NarrationProject


class JobCancelled(Exception):
    """Raised inside the worker to unwind a requested cancellation safely."""


def cancellation_requested(project_id):
    return NarrationProject.objects.filter(
        pk=project_id, cancel_requested=True,
    ).exists()


def check_job_cancellation(project_id):
    if cancellation_requested(project_id):
        raise JobCancelled("Traitement annulé à la demande de l’utilisateur.")


def mark_job_cancelled(project_id):
    NarrationProject.objects.filter(pk=project_id).update(
        status=NarrationProject.Status.DRAFT,
        cancel_requested=False,
        queued_at=None,
        started_at=None,
        status_message="Traitement annulé proprement.",
        error_message="",
        progress_percent=0,
        current_segment=0,
        total_segments=0,
        finished_at=timezone.now(),
    )


def cancellation_checker(project_id, interval=0.5):
    """Return a cheap-to-call callback that polls the database at most twice a second."""
    last_check = -interval
    requested = False

    def should_cancel():
        nonlocal last_check, requested
        now = time.monotonic()
        if now - last_check >= interval:
            requested = cancellation_requested(project_id)
            last_check = now
        return requested

    return should_cancel
