import subprocess
import sys
import time
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = "Lance le site et le worker de narration dans un seul terminal."

    def add_arguments(self, parser):
        parser.add_argument(
            "addrport",
            nargs="?",
            default="127.0.0.1:8000",
            help="Adresse locale du site (défaut : 127.0.0.1:8000).",
        )

    def handle(self, *args, **options):
        manage_py = Path(settings.BASE_DIR) / "manage.py"
        web = subprocess.Popen(
            [sys.executable, str(manage_py), "runserver", "--noreload", options["addrport"]],
            cwd=settings.BASE_DIR,
        )
        worker = None
        try:
            worker = subprocess.Popen(
                [sys.executable, str(manage_py), "run_narration_worker"],
                cwd=settings.BASE_DIR,
            )
            self.stdout.write(self.style.SUCCESS(
                f"Site et worker démarrés sur http://{options['addrport'].replace('127.0.0.1', 'localhost')}/"
            ))
            while web.poll() is None and worker.poll() is None:
                time.sleep(0.5)
            if web.poll() is not None:
                raise CommandError("Le serveur web s’est arrêté.")
            raise CommandError("Le worker de narration s’est arrêté.")
        except KeyboardInterrupt:
            self.stdout.write("\nArrêt du site et du worker…")
        finally:
            for process in (worker, web):
                if process is not None and process.poll() is None:
                    process.terminate()
            for process in (worker, web):
                if process is not None and process.poll() is None:
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()
