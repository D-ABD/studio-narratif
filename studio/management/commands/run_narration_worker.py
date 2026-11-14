import fcntl
import time

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import close_old_connections, transaction
from django.utils import timezone

from studio.models import NarrationProject
from studio.job_control import JobCancelled, mark_job_cancelled
from studio.services import prepare_project
from studio.video_services import render_project_video, render_video_assembly, render_video_narration


class Command(BaseCommand):
    help = "Traite en série la file persistante des narrations XTTS."

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true", help="Traite au plus un projet puis s’arrête.")
        parser.add_argument("--poll-seconds", type=float, default=2.0, help="Pause entre deux vérifications de la file.")

    def handle(self, *args, **options):
        lock_path = settings.MEDIA_ROOT / ".narration-worker.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock_file = lock_path.open("a+")
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise CommandError("Un worker de narration tourne déjà pour ce dossier média.") from exc

        self.stdout.write(self.style.SUCCESS("Worker prêt. En attente de narrations…"))
        self._recover_interrupted_jobs()
        while True:
            project_id = self._claim_next_job()
            if project_id is None:
                if options["once"]:
                    return
                time.sleep(max(0.25, options["poll_seconds"]))
                close_old_connections()
                continue

            self.stdout.write(f"Préparation du projet {project_id}…")
            try:
                job_type = NarrationProject.objects.filter(pk=project_id).values_list("job_type", flat=True).first()
                if job_type == NarrationProject.JobType.ASSEMBLY:
                    render_video_assembly(project_id)
                else:
                    prepare_project(project_id)
                if job_type == NarrationProject.JobType.VIDEO_NARRATION:
                    project = NarrationProject.objects.filter(pk=project_id).first()
                    if project and project.status == NarrationProject.Status.PROCESSING:
                        render_video_narration(project_id)
                elif job_type == NarrationProject.JobType.VIDEO:
                    project = NarrationProject.objects.filter(pk=project_id).first()
                    if project and project.status == NarrationProject.Status.PROCESSING:
                        render_project_video(project_id)
            except JobCancelled:
                mark_job_cancelled(project_id)
            except Exception as exc:  # prepare_project records errors; protect the worker from unexpected failures.
                NarrationProject.objects.filter(pk=project_id).update(
                    status=NarrationProject.Status.ERROR,
                    status_message="La préparation de la narration a échoué.",
                    error_message=str(exc)[:2000],
                    queued_at=None,
                    finished_at=timezone.now(),
                )
                self.stderr.write(self.style.ERROR(f"Projet {project_id} : {exc}"))
            finally:
                close_old_connections()
            if options["once"]:
                return

    def _recover_interrupted_jobs(self):
        now = timezone.now()
        NarrationProject.objects.filter(
            status=NarrationProject.Status.PROCESSING, cancel_requested=True,
        ).update(
            status=NarrationProject.Status.DRAFT,
            cancel_requested=False,
            queued_at=None,
            status_message="Traitement annulé après l’arrêt du worker.",
            started_at=None,
            error_message="",
            progress_percent=0,
            current_segment=0,
            total_segments=0,
            finished_at=now,
        )
        recovered = NarrationProject.objects.filter(
            status=NarrationProject.Status.PROCESSING, cancel_requested=False,
        ).update(
            status=NarrationProject.Status.QUEUED,
            queued_at=now,
            started_at=None,
            finished_at=None,
            progress_percent=0,
            current_segment=0,
            status_message="Repris après l’arrêt du worker.",
        )
        if recovered:
            self.stdout.write(f"{recovered} traitement(s) interrompu(s) remis dans la file.")

    @staticmethod
    def _claim_next_job():
        with transaction.atomic():
            project = (NarrationProject.objects
                       .filter(status=NarrationProject.Status.QUEUED, cancel_requested=False)
                       .order_by("queued_at", "id")
                       .first())
            if project is None:
                return None
            now = timezone.now()
            updated = NarrationProject.objects.filter(
                pk=project.pk, status=NarrationProject.Status.QUEUED, cancel_requested=False,
            ).update(
                status=NarrationProject.Status.PROCESSING,
                status_message="Chargement du moteur vocal…",
                started_at=now,
                finished_at=None,
                queued_at=None,
            )
            return project.pk if updated else None
