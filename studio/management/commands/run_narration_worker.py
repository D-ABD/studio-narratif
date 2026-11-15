import fcntl
import time

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import close_old_connections, transaction
from django.utils import timezone

from studio.models import NarrationProject, StudioBatch, StudioBatchItem
from studio.job_control import JobCancelled, mark_job_cancelled
from studio.services import generate_studio_batch, prepare_project
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

        self.stdout.write(self.style.SUCCESS(
            "Worker prêt · mode XTTS uniquement pour les projets, lots Studio en série. En attente de narrations…"
        ))
        self._recover_interrupted_jobs()
        self._recover_interrupted_studio_batches()
        while True:
            task = self._claim_next_task()
            if task is None:
                if options["once"]:
                    return
                time.sleep(max(0.25, options["poll_seconds"]))
                close_old_connections()
                continue

            task_type, task_id = task
            if task_type == "studio_batch":
                self.stdout.write(f"Génération du lot Studio {task_id}…")
                try:
                    generate_studio_batch(task_id)
                except Exception as exc:
                    StudioBatch.objects.filter(pk=task_id).update(
                        status=StudioBatch.Status.FAILED,
                        status_message="Le traitement du lot a échoué.",
                        error_message=str(exc)[:2000],
                        queued_at=None,
                        finished_at=timezone.now(),
                    )
                    self.stderr.write(self.style.ERROR(f"Lot Studio {task_id} : {exc}"))
                finally:
                    close_old_connections()
                if options["once"]:
                    return
                continue

            project_id = task_id
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

    def _recover_interrupted_studio_batches(self):
        now = timezone.now()
        reset_items = StudioBatchItem.objects.filter(
            status=StudioBatchItem.Status.PROCESSING,
        ).update(status=StudioBatchItem.Status.PENDING, updated_at=now)
        recovered = StudioBatch.objects.filter(status=StudioBatch.Status.PROCESSING).update(
            status=StudioBatch.Status.PENDING,
            queued_at=now,
            started_at=None,
            finished_at=None,
            status_message="Traitement interrompu · reprise automatique du lot.",
            updated_at=now,
        )
        activated_drafts = StudioBatch.objects.filter(status=StudioBatch.Status.DRAFT).update(
            status=StudioBatch.Status.PENDING,
            queued_at=now,
            status_message="Brouillon de lot repris · ajouté à la file.",
            updated_at=now,
        )
        if recovered or activated_drafts or reset_items:
            self.stdout.write(
                f"{recovered} lot(s) interrompu(s) repris · {activated_drafts} brouillon(s) ajouté(s) à la file · "
                f"{reset_items} narration(s) remise(s) en attente."
            )

    @staticmethod
    def _claim_next_task():
        """Claim the oldest project or standalone batch in one shared FIFO queue."""
        with transaction.atomic():
            project = (NarrationProject.objects
                       .filter(status=NarrationProject.Status.QUEUED, cancel_requested=False)
                       .order_by("queued_at", "id")
                       .first())
            batch = (StudioBatch.objects
                     .filter(status=StudioBatch.Status.PENDING)
                     .order_by("queued_at", "id")
                     .first())
            if project is None and batch is None:
                return None

            candidates = []
            if project is not None:
                candidates.append((project.queued_at or project.created_at, project.pk, "project"))
            if batch is not None:
                candidates.append((batch.queued_at or batch.created_at, batch.pk, "studio_batch"))
            _queued_at, task_id, task_type = min(candidates)
            now = timezone.now()
            if task_type == "studio_batch":
                updated = StudioBatch.objects.filter(
                    pk=task_id, status=StudioBatch.Status.PENDING,
                ).update(
                    status=StudioBatch.Status.PROCESSING,
                    status_message="Préparation du lot…",
                    started_at=now,
                    finished_at=None,
                    queued_at=None,
                    error_message="",
                )
            else:
                updated = NarrationProject.objects.filter(
                    pk=task_id, status=NarrationProject.Status.QUEUED, cancel_requested=False,
                ).update(
                    status=NarrationProject.Status.PROCESSING,
                    status_message="Chargement du moteur vocal…",
                    started_at=now,
                    finished_at=None,
                    queued_at=None,
                )
            return (task_type, task_id) if updated else None

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
