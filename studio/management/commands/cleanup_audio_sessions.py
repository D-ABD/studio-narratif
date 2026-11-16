from django.core.management.base import BaseCommand

from studio.audio_services import cleanup_expired_audio_sessions, cleanup_orphaned_audio_session_files


class Command(BaseCommand):
    help = "Supprime les sessions audio inactives depuis plus de 24 heures et leurs fichiers temporaires."

    def handle(self, *args, **options):
        deleted = cleanup_expired_audio_sessions()
        orphaned = cleanup_orphaned_audio_session_files()
        self.stdout.write(self.style.SUCCESS(
            f"{deleted} session(s) audio expirée(s) supprimée(s) ; "
            f"{orphaned} fichier(s) orphelin(s) supprimé(s)."
        ))
