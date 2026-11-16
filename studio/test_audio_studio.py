import json
import os
import tempfile
import time
from io import StringIO
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.core.management import call_command
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from .models import AudioAsset, AudioSession, BackgroundTrack


class AudioStudioViewTests(TestCase):
    def setUp(self):
        self.media_dir = tempfile.TemporaryDirectory(prefix="audio-studio-ui-tests-")
        self.settings_override = override_settings(MEDIA_ROOT=self.media_dir.name)
        self.settings_override.enable()
        self.addCleanup(self.settings_override.disable)
        self.addCleanup(self.media_dir.cleanup)
        user_model = get_user_model()
        self.user = user_model.objects.create_user(username="audio-studio-user", password="pass")
        self.other = user_model.objects.create_user(username="audio-studio-other", password="pass")
        self.client.force_login(self.user)

    def make_track(self, user, name="Piste source"):
        track = BackgroundTrack(user=user, name=name, duration=12.5)
        track.audio_file.save("source.mp3", ContentFile(b"track bytes"), save=False)
        track.save()
        return track

    def make_asset(self, user, name="Narration source"):
        asset = AudioAsset(user=user, name=name, duration=8.0, source=AudioAsset.Source.XTTS)
        asset.audio_file.save("source.wav", ContentFile(b"asset bytes"), save=False)
        asset.save()
        return asset

    def make_session(self, user=None):
        session = AudioSession(user=user or self.user, source_kind=AudioSession.SourceKind.UPLOAD)
        session.source_file.save("source.wav", ContentFile(b"source bytes"), save=False)
        session.save()
        return session

    def test_audio_studio_lists_all_three_source_paths_and_sidebar_link(self):
        response = self.client.get(reverse("audio_studio"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Importer un fichier")
        self.assertContains(response, "Depuis ma bibliothèque musicale")
        self.assertContains(response, "Depuis ma bibliothèque audio")
        self.assertContains(response, "Studio audio")

    def test_upload_source_creates_private_session(self):
        response = self.client.post(reverse("create_audio_studio_session"), {
            "source_kind": "upload",
            "audio_file": SimpleUploadedFile("Ma narration.wav", b"audio data", content_type="audio/wav"),
        })
        self.assertEqual(response.status_code, 302)
        session = AudioSession.objects.get(user=self.user)
        self.assertEqual(session.source_kind, AudioSession.SourceKind.UPLOAD)
        self.assertTrue(Path(session.source_file.path).exists())
        self.assertIn("source_name=Ma+narration", response.url)

    def test_from_music_and_from_audio_create_sessions(self):
        track = self.make_track(self.user)
        asset = self.make_asset(self.user)
        for source_kind, source in (("from_music", track), ("from_audio", asset)):
            with self.subTest(source_kind=source_kind):
                response = self.client.post(reverse("create_audio_studio_session"), {
                    "source_kind": source_kind, "source_id": source.pk,
                })
                self.assertEqual(response.status_code, 302)
        sessions = list(AudioSession.objects.filter(user=self.user).order_by("source_kind"))
        self.assertEqual({session.source_kind for session in sessions}, {
            AudioSession.SourceKind.FROM_MUSIC, AudioSession.SourceKind.FROM_AUDIO,
        })
        self.assertTrue(all(Path(session.source_file.path).exists() for session in sessions))

    def test_other_users_sources_return_404(self):
        track = self.make_track(self.other)
        asset = self.make_asset(self.other)
        for source_kind, source in (("from_music", track), ("from_audio", asset)):
            with self.subTest(source_kind=source_kind):
                response = self.client.post(reverse("create_audio_studio_session"), {
                    "source_kind": source_kind, "source_id": source.pk,
                })
                self.assertEqual(response.status_code, 404)

    def test_user_only_sees_and_opens_owned_sessions(self):
        owned = self.make_session(self.user)
        private = self.make_session(self.other)
        response = self.client.get(reverse("audio_studio"))
        self.assertContains(response, f"Session audio #{owned.pk}")
        self.assertNotContains(response, f"Session audio #{private.pk}")
        self.assertEqual(self.client.get(reverse("audio_studio_edit", args=[private.pk])).status_code, 404)

    def test_operations_can_be_added_removed_undone_and_reset(self):
        session = self.make_session()
        url = reverse("audio_studio_operations", args=[session.pk])
        version = session.operations_version

        def post(payload):
            nonlocal version
            response = self.client.post(
                url,
                json.dumps({**payload, "operations_version": version}),
                content_type="application/json",
            )
            if response.status_code == 200:
                version = response.json()["operations_version"]
            return response

        first = post({"action": "add", "operation": {"type": "volume", "params": {"percent": 80}}})
        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.json()["operations_version"], 1)
        post({"action": "add", "operation": {"type": "fade", "params": {"in": 1, "out": 1}}})
        post({"action": "remove", "index": 0})
        session.refresh_from_db()
        self.assertEqual(session.operations, [{"type": "fade", "params": {"in": 1, "out": 1}}])
        post({"action": "undo"})
        session.refresh_from_db()
        self.assertEqual(session.operations, [])
        post({"action": "add", "operation": {"type": "volume", "params": {"percent": 50}}})
        post({"action": "reset"})
        session.refresh_from_db()
        self.assertEqual(session.operations, [])
        self.assertEqual(session.operations_version, 6)

    def test_stale_operations_version_returns_409_without_overwriting(self):
        session = self.make_session()
        url = reverse("audio_studio_operations", args=[session.pk])
        first = self.client.post(url, json.dumps({
            "action": "add",
            "operations_version": 0,
            "operation": {"type": "volume", "params": {"percent": 80}},
        }), content_type="application/json")
        self.assertEqual(first.status_code, 200)

        stale = self.client.post(url, json.dumps({
            "action": "add",
            "operations_version": 0,
            "operation": {"type": "fade", "params": {"in": 1, "out": 1}},
        }), content_type="application/json")

        self.assertEqual(stale.status_code, 409)
        self.assertEqual(
            stale.json()["error"],
            "Un autre onglet a modifié les opérations. Recharge la page.",
        )
        session.refresh_from_db()
        self.assertEqual(session.operations, [{"type": "volume", "params": {"percent": 80}}])
        self.assertEqual(session.operations_version, 1)

    @patch("studio.views.apply_audio_operations")
    def test_preview_post_returns_audio_url_and_duration(self, apply_operations):
        session = self.make_session()

        def fake_apply(_source, _operations, output_path):
            output_path.write_bytes(b"encoded preview")
            return output_path, 7.25

        apply_operations.side_effect = fake_apply
        response = self.client.post(reverse("audio_studio_preview", args=[session.pk]))
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertIn("/media/audio-sessions/user_", payload["url"])
        self.assertEqual(payload["duration"], 7.25)
        session.refresh_from_db()
        self.assertTrue(session.current_preview_file.storage.exists(session.current_preview_file.name))

    @patch("studio.views.apply_audio_operations")
    def test_two_successive_previews_keep_the_previous_file_until_session_cleanup(self, apply_operations):
        session = self.make_session()
        counter = iter((b"first preview", b"second preview"))

        def fake_apply(_source, _operations, output_path):
            output_path.write_bytes(next(counter))
            return output_path, 7.25

        apply_operations.side_effect = fake_apply
        url = reverse("audio_studio_preview", args=[session.pk])

        first_response = self.client.post(url)
        self.assertEqual(first_response.status_code, 200)
        session.refresh_from_db()
        first_name = session.current_preview_file.name
        self.assertTrue(session.current_preview_file.storage.exists(first_name))

        second_response = self.client.post(url)
        self.assertEqual(second_response.status_code, 200)
        session.refresh_from_db()
        second_name = session.current_preview_file.name

        self.assertNotEqual(first_name, second_name)
        self.assertTrue(session.current_preview_file.storage.exists(first_name))
        self.assertTrue(session.current_preview_file.storage.exists(second_name))

    @patch("studio.views.apply_audio_operations")
    def test_cleanup_does_not_delete_session_during_active_preview(self, apply_operations):
        session = self.make_session()
        AudioSession.objects.filter(pk=session.pk).update(updated_at=timezone.now() - timedelta(hours=25))

        def fake_apply(_source, _operations, output_path):
            output_path.write_bytes(b"preview")
            from .audio_services import cleanup_expired_audio_sessions
            self.assertEqual(cleanup_expired_audio_sessions(user=self.user), 0)
            self.assertTrue(AudioSession.objects.filter(pk=session.pk).exists())
            return output_path, 7.25

        apply_operations.side_effect = fake_apply
        response = self.client.post(reverse("audio_studio_preview", args=[session.pk]))

        self.assertEqual(response.status_code, 200)
        self.assertTrue(AudioSession.objects.filter(pk=session.pk).exists())

    def test_editor_does_not_render_a_missing_preview_url(self):
        session = self.make_session()
        session.current_preview_file.save("missing.m4a", ContentFile(b"preview"), save=True)
        missing_name = session.current_preview_file.name
        session.current_preview_file.storage.delete(missing_name)

        response = self.client.get(reverse("audio_studio_edit", args=[session.pk]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Aperçu expiré, clique à nouveau sur")
        self.assertNotContains(response, missing_name)

    @patch("studio.views.probe_audio", return_value=(7.25, 48000))
    @patch("studio.views.apply_audio_operations")
    def test_save_creates_music_and_removes_session(self, apply_operations, _probe):
        session = self.make_session()

        def fake_apply(_source, _operations, output_path):
            output_path.write_bytes(b"preview for saved music")
            return output_path, 7.25

        apply_operations.side_effect = fake_apply
        preview = self.client.post(reverse("audio_studio_preview", args=[session.pk]))
        self.assertEqual(preview.status_code, 200)
        response = self.client.post(reverse("audio_studio_save", args=[session.pk]), {
            "name": "Musique travaillée", "source_name": "Narration source",
        })
        self.assertRedirects(response, reverse("musics"), fetch_redirect_response=False)
        track = BackgroundTrack.objects.get(user=self.user, name="Musique travaillée")
        self.assertEqual(track.duration, 7.25)
        self.assertTrue(track.audio_file.storage.exists(track.audio_file.name))
        self.assertFalse(AudioSession.objects.filter(pk=session.pk).exists())

    @patch("studio.views.probe_audio", return_value=(7.25, 48000))
    @patch("studio.views.apply_audio_operations")
    def test_duplicate_save_posts_create_at_most_one_track_and_second_returns_404(
        self, apply_operations, _probe,
    ):
        session = self.make_session()

        def fake_apply(_source, _operations, output_path):
            output_path.write_bytes(b"preview for double save")
            return output_path, 7.25

        apply_operations.side_effect = fake_apply
        preview = self.client.post(reverse("audio_studio_preview", args=[session.pk]))
        self.assertEqual(preview.status_code, 200)
        save_url = reverse("audio_studio_save", args=[session.pk])
        payload = {"name": "Saved once"}

        first = self.client.post(save_url, payload)
        second = self.client.post(save_url, payload)

        self.assertEqual(first.status_code, 302)
        self.assertEqual(second.status_code, 404)
        self.assertEqual(BackgroundTrack.objects.filter(user=self.user, name="Saved once").count(), 1)

    def test_save_requires_a_preview_matching_current_operations(self):
        session = self.make_session()
        session.current_preview_file.save("old.m4a", ContentFile(b"old preview"), save=True)
        response = self.client.post(reverse("audio_studio_save", args=[session.pk]), {"name": "Track"})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, reverse("audio_studio_edit", args=[session.pk]))
        self.assertFalse(BackgroundTrack.objects.filter(user=self.user, name="Track").exists())

    @patch("studio.views.apply_audio_operations", side_effect=RuntimeError("FFmpeg error"))
    def test_preview_failure_keeps_operations_and_previous_preview(self, _apply):
        session = self.make_session()
        session.operations = [{"type": "volume", "params": {"percent": 80}}]
        session.current_preview_file.save("previous.m4a", ContentFile(b"previous preview"), save=True)
        old_name = session.current_preview_file.name

        response = self.client.post(reverse("audio_studio_preview", args=[session.pk]))

        self.assertEqual(response.status_code, 400)
        self.assertIn("Traitement audio échoué", response.json()["error"])
        session.refresh_from_db()
        self.assertEqual(session.operations, [{"type": "volume", "params": {"percent": 80}}])
        self.assertEqual(session.current_preview_file.name, old_name)
        self.assertTrue(session.current_preview_file.storage.exists(old_name))

    def test_editor_disables_save_until_preview_and_shows_stale_warning(self):
        session = self.make_session()
        response = self.client.get(reverse("audio_studio_edit", args=[session.pk]))
        self.assertContains(response, "Sauvegarder dans la bibliothèque musique")
        self.assertContains(response, "disabled")
        self.assertContains(response, 'data-operations-version="0"')
        self.assertContains(response, "data-audio-player-error")
        app_js = (Path(__file__).parent / "static" / "studio" / "app.js").read_text()
        self.assertIn("Impossible de lire l’aperçu. Clique à nouveau sur Écouter.", app_js)
        session.operations = [{"type": "volume", "params": {"percent": 80}}]
        session.current_preview_file.save("old.m4a", ContentFile(b"old preview"), save=True)
        response = self.client.get(reverse("audio_studio_edit", args=[session.pk]))
        self.assertContains(response, "Les opérations ont changé depuis le dernier aperçu")

    def test_get_opportunistically_cleans_sessions_idle_over_24_hours(self):
        expired = self.make_session()
        AudioSession.objects.filter(pk=expired.pk).update(updated_at=timezone.now() - timedelta(hours=25))
        response = self.client.get(reverse("audio_studio"))
        self.assertEqual(response.status_code, 200)
        self.assertFalse(AudioSession.objects.filter(pk=expired.pk).exists())

    def test_cleanup_management_command_removes_expired_sessions_and_old_orphans(self):
        expired = self.make_session(self.user)
        active = self.make_session()
        AudioSession.objects.filter(pk=expired.pk).update(updated_at=timezone.now() - timedelta(hours=25))
        expired_files = [
            name for name in (expired.source_file.name, expired.current_preview_file.name)
            if name
        ]
        orphan_name = default_storage.save(
            f"audio-sessions/user_{self.user.pk}/session_999_orphan.m4a",
            ContentFile(b"orphan"),
        )
        orphan_path = Path(default_storage.path(orphan_name))
        old_epoch = time.time() - timedelta(hours=25).total_seconds()
        os.utime(orphan_path, (old_epoch, old_epoch))
        output = StringIO()

        with self.captureOnCommitCallbacks(execute=True):
            call_command("cleanup_audio_sessions", stdout=output)

        self.assertFalse(AudioSession.objects.filter(pk=expired.pk).exists())
        self.assertTrue(AudioSession.objects.filter(pk=active.pk).exists())
        self.assertTrue(all(not default_storage.exists(name) for name in expired_files))
        self.assertFalse(default_storage.exists(orphan_name))
        self.assertIn("1 session(s) audio expirée(s) supprimée(s)", output.getvalue())
        self.assertIn("1 fichier(s) orphelin(s) supprimé(s)", output.getvalue())

