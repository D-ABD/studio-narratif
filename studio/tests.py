import importlib.util
import json
from io import BytesIO, StringIO
import sys
import math
import shutil
import struct
import subprocess
import tempfile
import types
import wave
from contextlib import nullcontext
from datetime import timedelta
from pathlib import Path
from unittest.mock import Mock, patch

from django.contrib.auth import get_user_model
from django.core.files.base import ContentFile
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.utils import timezone
from PIL import Image

from .models import (
    AudioAsset, AudioSession, BackgroundTrack, NarrationProject, NarrationSegment, ProjectFolder,
    StudioBatch, StudioBatchItem, Voice,
    default_animation_settings,
)
from .services import (
    _assemble_wav, _get_speaker_conditioning, _group_words, _prepare_segment, align_audio,
    cache_key_for, srt_text, wav_duration,
)
from . import services


class MediaTestCase(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username="test-owner", password="SafePass123!x")
        self.client.force_login(self.user)
        self.media_dir = tempfile.TemporaryDirectory(prefix="studio-voix-tests-")
        self.override = override_settings(MEDIA_ROOT=self.media_dir.name)
        self.override.enable()
        self.addCleanup(self.override.disable)
        self.addCleanup(self.media_dir.cleanup)

    def make_voice(self, name="Voix test", category=Voice.Category.OTHER):
        voice = Voice(user=self.user, name=name, category=category, consent_confirmed=True)
        voice.sample.save("sample.wav", ContentFile(b"sample voice bytes"), save=True)
        return voice


class VoiceLibraryTests(MediaTestCase):
    def test_voice_requires_explicit_consent(self):
        upload = SimpleUploadedFile("sample.wav", b"audio", content_type="audio/wav")
        response = self.client.post("/voices/create/", {"name": "Voix", "category": "homme", "sample": upload})
        self.assertEqual(response.status_code, 302)
        self.assertFalse(Voice.objects.exists())

    @patch("studio.views.normalize_voice_sample", return_value=b"normalized wav data")
    def test_create_edit_and_delete_voice(self, _convert):
        upload = SimpleUploadedFile("sample.mp3", b"audio", content_type="audio/mpeg")
        response = self.client.post("/voices/create/", {
            "name": "Coach", "category": "shiba", "voice_consent": "on", "sample": upload,
        })
        self.assertEqual(response.status_code, 302)
        voice = Voice.objects.get()
        self.assertEqual(voice.name, "Coach")
        self.assertTrue(voice.consent_confirmed)
        self.assertTrue(voice.sample_sha256)

        self.client.post(f"/voices/{voice.pk}/edit/", {"name": "Coach calme", "category": "autre"})
        voice.refresh_from_db()
        self.assertEqual(voice.name, "Coach calme")
        self.assertEqual(voice.category, Voice.Category.OTHER)

        self.client.post(f"/voices/{voice.pk}/delete/", {"confirm": "yes"})
        self.assertFalse(Voice.objects.exists())


class BackgroundMusicLibraryTests(MediaTestCase):
    def make_track(self, name="Ambiance"):
        track = BackgroundTrack(user=self.user, name=name, duration=42.5)
        track.audio_file.save("sample.mp3", ContentFile(b"test audio bytes"), save=True)
        return track

    def test_music_library_is_isolated_by_user(self):
        own_track = self.make_track("À moi")
        other_user = get_user_model().objects.create_user(username="other-owner", password="SafePass123!x")
        other_track = BackgroundTrack(user=other_user, name="Privée", duration=12.0)
        other_track.audio_file.save("other.ogg", ContentFile(b"other audio"), save=True)

        response = self.client.get("/musics/")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "À moi")
        self.assertNotContains(response, "Privée")
        self.assertEqual(list(response.context["background_tracks"]), [own_track])
        self.assertEqual(self.client.post(f"/musics/{other_track.pk}/delete/", {"confirm": "yes"}).status_code, 404)
        self.assertTrue(BackgroundTrack.objects.filter(pk=other_track.pk).exists())

    @patch("studio.views._probe_music_duration", return_value=71.25)
    def test_upload_music_validates_extension_and_detects_duration(self, probe):
        upload = SimpleUploadedFile("theme.mp3", b"audio payload", content_type="audio/mpeg")
        response = self.client.post("/musics/upload/", {"name": "Thème", "audio_file": upload})

        self.assertRedirects(response, "/musics/")
        track = BackgroundTrack.objects.get(user=self.user)
        self.assertEqual(track.name, "Thème")
        self.assertEqual(track.duration, 71.25)
        self.assertTrue(track.audio_file.name.startswith(f"musics/user_{self.user.pk}/"))
        self.assertEqual(Path(track.audio_file.name).suffix, ".mp3")
        probe.assert_called_once()

    def test_deleting_music_used_by_project_detaches_and_invalidates_video(self):
        track = self.make_track()
        project = NarrationProject.objects.create(
            user=self.user, title="Avec musique", background_track=track,
            status=NarrationProject.Status.READY,
        )
        project.video_file.save("ready.mp4", ContentFile(b"old video output"), save=True)
        old_video_path = Path(project.video_file.path)

        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(f"/musics/{track.pk}/delete/", {"confirm": "yes"})

        self.assertRedirects(response, "/musics/")
        project.refresh_from_db()
        self.assertIsNone(project.background_track)
        self.assertFalse(project.video_file)
        self.assertEqual(project.status, NarrationProject.Status.READY)
        self.assertIn("musique associée a été supprimée", project.status_message)
        self.assertFalse(BackgroundTrack.objects.filter(pk=track.pk).exists())
        self.assertFalse(old_video_path.exists())

    @patch("studio.views._probe_music_duration")
    def test_music_upload_rejects_unsupported_format_and_oversize_before_probing(self, probe):
        unsupported = SimpleUploadedFile("theme.flac", b"audio", content_type="audio/flac")
        response = self.client.post("/musics/upload/", {"audio_file": unsupported})
        self.assertRedirects(response, "/musics/")
        self.assertFalse(BackgroundTrack.objects.exists())

        with patch("studio.views.MAX_MUSIC_BYTES", 4):
            oversized = SimpleUploadedFile("large.mp3", b"12345", content_type="audio/mpeg")
            response = self.client.post("/musics/upload/", {"audio_file": oversized})
        self.assertRedirects(response, "/musics/")
        self.assertFalse(BackgroundTrack.objects.exists())
        probe.assert_not_called()


class AudioAssetLibraryTests(MediaTestCase):
    def make_asset(self, user, name):
        asset = AudioAsset(user=user, name=name, duration=12.5, source=AudioAsset.Source.UPLOAD)
        asset.audio_file.save("future-library.mp3", ContentFile(b"audio bytes"), save=True)
        return asset

    def test_audio_library_lists_only_owner_assets_with_duration_and_preview(self):
        own_asset = self.make_asset(self.user, "Mon audio")
        newer_asset = self.make_asset(self.user, "Audio récent")
        AudioAsset.objects.filter(pk=own_asset.pk).update(created_at=timezone.now() - timedelta(days=1))
        other_user = get_user_model().objects.create_user(username="audio-owner", password="SafePass123!x")
        self.make_asset(other_user, "Audio privé")

        response = self.client.get("/audios/")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Mon audio")
        self.assertContains(response, "Audio récent")
        self.assertNotContains(response, "Audio privé")
        self.assertEqual(list(response.context["audio_assets"]), [newer_asset, own_asset])
        self.assertEqual(response.context["audio_assets"][1].duration_mmss, "00:12")
        self.assertContains(response, "Uploadé")
        self.assertContains(response, "<audio", html=False)

    @patch("studio.views._probe_music_duration", return_value=71.25)
    def test_upload_creates_owned_audio_asset_and_stores_file(self, probe):
        upload = SimpleUploadedFile("Narration.m4a", b"valid audio bytes", content_type="audio/mp4")

        response = self.client.post("/audios/upload/", {"audio_file": upload})

        self.assertRedirects(response, "/audios/")
        asset = AudioAsset.objects.get(user=self.user)
        self.assertEqual(asset.name, "Narration")
        self.assertEqual(asset.duration, 71.25)
        self.assertEqual(asset.source, AudioAsset.Source.UPLOAD)
        self.assertTrue(asset.audio_file.name.startswith(f"audios/user_{self.user.pk}/"))
        self.assertTrue(Path(asset.audio_file.path).is_file())
        probe.assert_called_once()

    @patch("studio.views._probe_music_duration")
    def test_upload_rejects_invalid_format_without_creating_asset_or_file(self, probe):
        upload = SimpleUploadedFile("narration.flac", b"audio bytes", content_type="audio/flac")

        response = self.client.post("/audios/upload/", {"audio_file": upload})

        self.assertEqual(response.status_code, 400)
        self.assertFalse(AudioAsset.objects.exists())
        self.assertEqual(list(Path(self.media_dir.name).rglob("*")), [])
        probe.assert_not_called()

    @patch("studio.views._probe_music_duration")
    def test_oversize_upload_is_rejected_before_disk_write_and_upload_temp_is_cleaned(self, probe):
        upload_tmp_dir = Path(self.media_dir.name).parent / f"{Path(self.media_dir.name).name}-uploads"
        upload_tmp_dir.mkdir()
        self.addCleanup(lambda: __import__("shutil").rmtree(upload_tmp_dir, ignore_errors=True))
        upload = SimpleUploadedFile("large.mp3", b"x" * 1025, content_type="audio/mpeg")

        with patch("studio.views.MAX_AUDIO_ASSET_BYTES", 1024), override_settings(
            FILE_UPLOAD_MAX_MEMORY_SIZE=1,
            FILE_UPLOAD_TEMP_DIR=str(upload_tmp_dir),
        ):
            response = self.client.post("/audios/upload/", {"audio_file": upload})

        self.assertEqual(response.status_code, 400)
        self.assertFalse(AudioAsset.objects.exists())
        self.assertEqual(list(Path(self.media_dir.name).rglob("*")), [])
        self.assertEqual(list(upload_tmp_dir.iterdir()), [])
        probe.assert_not_called()

    @patch("studio.views._probe_music_duration")
    def test_audio_upload_rejects_empty_file_and_unusable_audio(self, probe):
        empty = SimpleUploadedFile("empty.wav", b"", content_type="audio/wav")
        response = self.client.post("/audios/upload/", {"audio_file": empty})
        self.assertEqual(response.status_code, 400)
        self.assertFalse(AudioAsset.objects.exists())

        probe.side_effect = ValueError("Impossible de détecter la durée de cette musique.")
        invalid_audio = SimpleUploadedFile("broken.wav", b"not really audio", content_type="audio/wav")
        response = self.client.post("/audios/upload/", {"audio_file": invalid_audio})
        self.assertEqual(response.status_code, 400)
        self.assertFalse(AudioAsset.objects.exists())
        self.assertEqual(list(Path(self.media_dir.name).rglob("*")), [])

    def test_project_audio_upload_endpoint_is_removed(self):
        project = NarrationProject.objects.create(user=self.user, title="XTTS uniquement")

        response = self.client.post(f"/projects/{project.pk}/audio/", {})

        self.assertEqual(response.status_code, 404)

    def test_rename_validates_name_and_is_owner_scoped(self):
        asset = self.make_asset(self.user, "Avant")

        response = self.client.post(f"/audios/{asset.pk}/rename/", {"name": "  Après  "})
        self.assertRedirects(response, "/audios/")
        asset.refresh_from_db()
        self.assertEqual(asset.name, "Après")

        for invalid_name in ("   ", "x" * 101):
            response = self.client.post(f"/audios/{asset.pk}/rename/", {"name": invalid_name})
            self.assertEqual(response.status_code, 400)
            asset.refresh_from_db()
            self.assertEqual(asset.name, "Après")

        other_user = get_user_model().objects.create_user(username="rename-owner", password="SafePass123!x")
        other_asset = self.make_asset(other_user, "Privé")
        response = self.client.post(f"/audios/{other_asset.pk}/rename/", {"name": "Intrusion"})
        self.assertEqual(response.status_code, 404)
        other_asset.refresh_from_db()
        self.assertEqual(other_asset.name, "Privé")

    def test_delete_requires_confirmation_and_refuses_project_references(self):
        asset = self.make_asset(self.user, "Protégé")
        path = Path(asset.audio_file.path)

        response = self.client.post(f"/audios/{asset.pk}/delete/", {})
        self.assertEqual(response.status_code, 400)
        self.assertContains(response, "Confirmation manquante", status_code=400)
        self.assertTrue(AudioAsset.objects.filter(pk=asset.pk).exists())
        self.assertTrue(path.exists())

        project = NarrationProject.objects.create(user=self.user, title="Utilise cet audio")
        asset.project = project
        asset.save(update_fields=["project", "updated_at"])
        response = self.client.post(f"/audios/{asset.pk}/delete/", {"confirm": "1"})
        self.assertEqual(response.status_code, 400)
        self.assertContains(response, "référencé par un projet", status_code=400)
        self.assertTrue(AudioAsset.objects.filter(pk=asset.pk).exists())
        self.assertTrue(path.exists())

    def test_delete_removes_database_row_then_file_after_commit(self):
        asset = self.make_asset(self.user, "À supprimer")
        path = Path(asset.audio_file.path)

        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(f"/audios/{asset.pk}/delete/", {"confirm": "1"})

        self.assertRedirects(response, "/audios/")
        self.assertFalse(AudioAsset.objects.filter(pk=asset.pk).exists())
        self.assertFalse(path.exists())

    def test_delete_missing_file_logs_warning_but_succeeds(self):
        asset = self.make_asset(self.user, "Fichier absent")
        Path(asset.audio_file.path).unlink()

        with self.assertLogs("studio.views", level="WARNING") as logs:
            with self.captureOnCommitCallbacks(execute=True):
                response = self.client.post(f"/audios/{asset.pk}/delete/", {"confirm": "1"})

        self.assertRedirects(response, "/audios/")
        self.assertFalse(AudioAsset.objects.filter(pk=asset.pk).exists())
        self.assertIn("déjà absent", " ".join(logs.output))

    def test_delete_is_owner_scoped_and_login_is_required(self):
        other_user = get_user_model().objects.create_user(username="delete-owner", password="SafePass123!x")
        other_asset = self.make_asset(other_user, "Privé")
        response = self.client.post(f"/audios/{other_asset.pk}/delete/", {"confirm": "1"})
        self.assertEqual(response.status_code, 404)
        self.assertTrue(AudioAsset.objects.filter(pk=other_asset.pk).exists())

        self.client.logout()
        for url in ("/audios/", "/audios/upload/", f"/audios/{other_asset.pk}/rename/", f"/audios/{other_asset.pk}/delete/"):
            response = self.client.post(url, {"confirm": "1"}) if url != "/audios/" else self.client.get(url)
            self.assertEqual(response.status_code, 302)
            self.assertIn("/login/", response.url)


class StudioBatchGenerationTests(MediaTestCase):
    def make_batch(self, texts, *, voice=None, name_prefix="Lot test", status=StudioBatch.Status.PROCESSING):
        voice = voice or self.make_voice()
        batch = StudioBatch.objects.create(
            user=self.user,
            voice=voice,
            name_prefix=name_prefix,
            status=status,
            total_items=len(texts),
            queued_at=timezone.now() if status == StudioBatch.Status.PENDING else None,
        )
        StudioBatchItem.objects.bulk_create([
            StudioBatchItem(
                batch=batch,
                position=index,
                text=text,
                asset_name=f"{name_prefix} {index + 1:02d}",
            )
            for index, text in enumerate(texts)
        ])
        return batch

    @staticmethod
    def write_test_wav(text, voice, destination):
        with wave.open(str(destination), "wb") as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(24000)
            output.writeframes(b"\0\0" * 24000)

    @staticmethod
    def aligned_captions(audio_path, text):
        first_word = text.split()[0]
        return [{
            "start": 0.05,
            "end": 0.5,
            "text": first_word,
            "words": [{"word": first_word, "start": 0.05, "end": 0.5}],
        }]

    def test_one_text_creates_xtts_asset_with_timings_without_transcription_model(self):
        voice = self.make_voice()
        batch = self.make_batch(["Bonjour le monde."], voice=voice, name_prefix="Test")
        fake_torch = types.ModuleType("torch")
        fake_torch.cuda = types.SimpleNamespace(is_available=lambda: False)
        fake_whisperx = types.ModuleType("whisperx")
        fake_whisperx.load_audio = Mock(return_value=[0.0] * 16000)
        fake_whisperx.load_align_model = Mock(return_value=(object(), object()))
        fake_whisperx.align = Mock(return_value={
            "word_segments": [{"word": "Bonjour", "start": 0.05, "end": 0.5}],
        })
        fake_whisperx.load_model = Mock(side_effect=AssertionError("La transcription ne doit pas être chargée."))

        with (
            patch.object(services, "_ALIGNMENT_MODEL", None),
            patch.object(services, "_ALIGNMENT_METADATA", None),
            patch.object(services, "_ALIGNMENT_DEVICE", None),
            patch.object(services, "_ALIGNMENT_BACKEND", None),
            patch.dict(sys.modules, {"torch": fake_torch, "whisperx": fake_whisperx}),
            patch("studio.services._synthesize_segment", side_effect=self.write_test_wav) as synthesize,
        ):
            services.generate_studio_batch(batch.pk)

        asset = AudioAsset.objects.get()
        self.assertEqual(asset.source, AudioAsset.Source.XTTS)
        self.assertEqual(asset.text, "Bonjour le monde.")
        self.assertEqual(asset.voice, voice)
        self.assertEqual(asset.duration, 1.0)
        self.assertEqual(asset.timings["version"], 1)
        self.assertEqual(asset.timings["words"], [{"word": "Bonjour", "start": 0.05, "end": 0.5}])
        self.assertEqual(asset.timings["segments"], [{"text": "Bonjour", "start": 0.05, "end": 0.5}])
        self.assertEqual(asset.timings["scene_boundaries"], [{"scene_index": 0, "start": 0.0, "end": 1.0}])
        self.assertTrue(Path(asset.audio_file.path).is_file())
        self.assertEqual(batch.items.get().status, StudioBatchItem.Status.COMPLETED)
        batch.refresh_from_db()
        self.assertEqual(batch.status, StudioBatch.Status.COMPLETED)
        synthesize.assert_called_once()
        fake_whisperx.load_align_model.assert_called_once()
        fake_whisperx.load_model.assert_not_called()

    def test_three_texts_create_three_assets_serially(self):
        batch = self.make_batch(["Un texte.", "Deuxième texte.", "Troisième texte."], name_prefix="Série")

        with patch("studio.services._synthesize_segment", side_effect=self.write_test_wav) as synthesize:
            with patch("studio.services.align_audio", side_effect=self.aligned_captions) as align:
                services.generate_studio_batch(batch.pk)

        assets = list(AudioAsset.objects.order_by("id"))
        self.assertEqual(len(assets), 3)
        self.assertEqual([asset.name for asset in assets], ["Série 01", "Série 02", "Série 03"])
        self.assertTrue(all(asset.source == AudioAsset.Source.XTTS and asset.timings for asset in assets))
        self.assertEqual(synthesize.call_count, 3)
        self.assertEqual(align.call_count, 3)
        batch.refresh_from_db()
        self.assertEqual(batch.completed_items, 3)
        self.assertEqual(batch.progress_percent, 100)
        self.assertEqual(batch.status, StudioBatch.Status.COMPLETED)

    def test_identical_project_text_cache_is_reused_without_xtts_or_whisperx(self):
        voice = self.make_voice()
        text = "Phrase déjà générée."
        project = NarrationProject.objects.create(user=self.user, title="Projet cache", voice=voice)
        segment = NarrationSegment.objects.create(project=project, position=0, text=text)
        cache_key = services.cache_key_for(text, voice, sample_digest=services._voice_sample_digest(voice))
        with patch("studio.services._synthesize_segment", side_effect=self.write_test_wav) as synthesize:
            with patch("studio.services.align_audio", side_effect=self.aligned_captions) as align:
                services._prepare_segment(segment, voice, cache_key)
        synthesize.assert_called_once()
        align.assert_called_once()

        standalone_batch = self.make_batch([text], voice=voice, name_prefix="Studio")
        with patch("studio.services._synthesize_segment") as synthesize_again:
            with patch("studio.services.align_audio") as align_again:
                services.generate_studio_batch(standalone_batch.pk)

        synthesize_again.assert_not_called()
        align_again.assert_not_called()
        studio_asset = AudioAsset.objects.get(name="Studio 01")
        self.assertEqual(studio_asset.text, text)
        self.assertEqual(studio_asset.timings["scene_boundaries"][0]["scene_index"], 0)

        other_project = NarrationProject.objects.create(user=self.user, title="Studio vers projet", voice=voice)
        other_segment = NarrationSegment.objects.create(project=other_project, position=0, text=text)
        with patch("studio.services._synthesize_segment") as project_synthesize:
            with patch("studio.services.align_audio") as project_align:
                services._prepare_segment(other_segment, voice, cache_key)
        project_synthesize.assert_not_called()
        project_align.assert_not_called()

    def test_form_and_batch_status_are_isolated_and_validate_user_owned_voice(self):
        own_batch = self.make_batch(["Texte privé."], status=StudioBatch.Status.DRAFT)
        other_user = get_user_model().objects.create_user(username="studio-owner", password="SafePass123!x")
        other_voice = Voice(user=other_user, name="Autre voix", consent_confirmed=True)
        other_voice.sample.save("other.wav", ContentFile(b"other sample"), save=True)
        other_batch = StudioBatch.objects.create(
            user=other_user, voice=other_voice, total_items=0, status=StudioBatch.Status.PENDING,
        )

        page = self.client.get("/studio/")
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, "Lot test")
        self.assertNotContains(page, "Autre voix")
        self.assertContains(page, 'href="/studio/"')
        self.assertEqual(list(page.context["studio_batches"]), [own_batch])
        self.assertEqual(self.client.get(f"/studio/batches/{own_batch.pk}/status/").json()["id"], own_batch.pk)
        self.assertEqual(self.client.get(f"/studio/batches/{other_batch.pk}/status/").status_code, 404)

        foreign_voice_post = self.client.post("/studio/batches/create/", {
            "voice_id": other_voice.pk, "texts": ["Ne doit pas être enregistré."],
        })
        self.assertEqual(foreign_voice_post.status_code, 400)
        self.assertFalse(StudioBatch.objects.filter(user=self.user, status=StudioBatch.Status.PENDING).exists())

        response = self.client.post("/studio/batches/create/", {
            "voice_id": own_batch.voice_id, "texts": ["Valide.", "   "],
        })
        self.assertEqual(response.status_code, 400)
        response = self.client.post("/studio/batches/create/", {
            "voice_id": own_batch.voice_id, "texts": [f"Texte {index}" for index in range(21)],
        })
        self.assertEqual(response.status_code, 400)

    def test_voice_cannot_be_deleted_while_its_studio_batch_is_active(self):
        voice = self.make_voice()
        self.make_batch(["Encore en attente."], voice=voice, status=StudioBatch.Status.PENDING)

        response = self.client.post(f"/voices/{voice.pk}/delete/", {"confirm": "yes"})

        self.assertEqual(response.status_code, 302)
        self.assertTrue(Voice.objects.filter(pk=voice.pk).exists())

    def test_worker_requeues_processing_items_after_crash_and_claims_the_batch(self):
        from .management.commands.run_narration_worker import Command

        batch = self.make_batch(["À reprendre."], status=StudioBatch.Status.PROCESSING)
        item = batch.items.get()
        item.status = StudioBatchItem.Status.PROCESSING
        item.save(update_fields=["status", "updated_at"])
        command = Command()
        command.stdout = StringIO()

        command._recover_interrupted_studio_batches()

        batch.refresh_from_db()
        item.refresh_from_db()
        self.assertEqual(batch.status, StudioBatch.Status.PENDING)
        self.assertIsNotNone(batch.queued_at)
        self.assertEqual(item.status, StudioBatchItem.Status.PENDING)
        self.assertEqual(Command._claim_next_task(), ("studio_batch", batch.pk))
        batch.refresh_from_db()
        self.assertEqual(batch.status, StudioBatch.Status.PROCESSING)

    def test_pending_batch_can_be_cancelled_and_all_items_are_marked_cancelled(self):
        batch = self.make_batch(["Texte un.", "Texte deux."], status=StudioBatch.Status.PENDING)

        response = self.client.post(f"/studio/batches/{batch.pk}/cancel/")

        self.assertRedirects(response, "/studio/", fetch_redirect_response=False)
        batch.refresh_from_db()
        self.assertEqual(batch.status, StudioBatch.Status.CANCELLED)
        self.assertIsNotNone(batch.finished_at)
        self.assertEqual(
            set(batch.items.values_list("status", flat=True)),
            {StudioBatchItem.Status.CANCELLED},
        )

    def test_cancel_batch_is_owner_scoped_and_only_active_batches_can_be_cancelled(self):
        other_user = get_user_model().objects.create_user(username="cancel-private", password="SafePass123!x")
        foreign_batch = StudioBatch.objects.create(
            user=other_user, status=StudioBatch.Status.PENDING, total_items=1,
        )
        own_completed = self.make_batch(["Déjà terminé."], status=StudioBatch.Status.COMPLETED)

        self.assertEqual(self.client.post(f"/studio/batches/{foreign_batch.pk}/cancel/").status_code, 404)
        self.assertEqual(self.client.post(f"/studio/batches/{own_completed.pk}/cancel/").status_code, 302)
        foreign_batch.refresh_from_db()
        own_completed.refresh_from_db()
        self.assertEqual(foreign_batch.status, StudioBatch.Status.PENDING)
        self.assertEqual(own_completed.status, StudioBatch.Status.COMPLETED)

    def test_processing_cancellation_finishes_current_item_and_preserves_its_audio(self):
        batch = self.make_batch(["En cours.", "À annuler 2.", "À annuler 3."], name_prefix="Conservation")

        def synthesize_and_cancel(text, voice, destination):
            self.write_test_wav(text, voice, destination)
            StudioBatch.objects.filter(pk=batch.pk).update(
                status=StudioBatch.Status.CANCELLED,
                status_message="Annulation demandée.",
            )

        with patch("studio.services._synthesize_segment", side_effect=synthesize_and_cancel):
            with patch("studio.services.align_audio", side_effect=self.aligned_captions):
                services.generate_studio_batch(batch.pk)

        batch.refresh_from_db()
        items = list(batch.items.order_by("position"))
        self.assertEqual(batch.status, StudioBatch.Status.CANCELLED)
        self.assertEqual(items[0].status, StudioBatchItem.Status.COMPLETED)
        self.assertIsNotNone(items[0].audio_asset_id)
        self.assertTrue(Path(items[0].audio_asset.audio_file.path).is_file())
        self.assertEqual(
            [item.status for item in items[1:]],
            [StudioBatchItem.Status.CANCELLED, StudioBatchItem.Status.CANCELLED],
        )
        self.assertEqual(AudioAsset.objects.count(), 1)

    def test_cancelled_batches_are_not_requeued_after_worker_crash(self):
        from .management.commands.run_narration_worker import Command

        batch = self.make_batch(["Narration active."], status=StudioBatch.Status.CANCELLED)
        item = batch.items.get()
        item.status = StudioBatchItem.Status.PROCESSING
        item.save(update_fields=["status", "updated_at"])
        command = Command()
        command.stdout = StringIO()

        command._recover_interrupted_studio_batches()

        batch.refresh_from_db()
        item.refresh_from_db()
        self.assertEqual(batch.status, StudioBatch.Status.CANCELLED)
        s…14322 tokens truncated…us.READY, project.error_message)
                self.assertEqual(len(intermediate_metadata), 1)
                self.assertFalse(intermediate_metadata[0]["has_audio"])
                self.assertEqual(len(commands), 3)
                intermediate = commands[0]
                self.assertIn("-an", intermediate)
                self.assertEqual(intermediate[intermediate.index("-map") + 1], "0:v:0")
                video_filters = intermediate[intermediate.index("-vf") + 1]
                self.assertIn("scale=128:128:force_original_aspect_ratio=increase", video_filters)
                self.assertIn("crop=128:128", video_filters)
                self.assertIn("fps=24,setsar=1", video_filters)
                self.assertEqual("tpad=stop_mode=clone" in video_filters, expect_padding)
                self.assertEqual(intermediate[intermediate.index("-t") + 1], "0.500")

                final_command = commands[2]
                self.assertIn(asset.audio_file.path, final_command)
                self.assertEqual(final_command[final_command.index("-map") + 1], "0:v:0")
                self.assertEqual(final_command[final_command.index("-map") + 3], "1:a:0")
                final_metadata = video_services._probe_clip(project.video_file.path)
                self.assertTrue(final_metadata["has_audio"])
                self.assertAlmostEqual(final_metadata["duration"], boundary_duration, delta=0.12)

    @patch("studio.video_services._ffmpeg_path", return_value="ffmpeg")
    @patch("studio.video_services._video_encoder", return_value="libx264")
    @patch("studio.video_services._run_ffmpeg")
    def test_assembly_renders_one_global_ass_and_uses_only_boundary_images(self, run_ffmpeg, _encoder, _ffmpeg):
        from .video_services import render_project_assembly

        project, visual, ignored = self.make_assembly_project()
        commands = []
        subtitle_texts = []

        def create_output(command, cwd, *_args, **_kwargs):
            commands.append(list(command))
            subtitle_file = Path(cwd) / "assembly.ass"
            if subtitle_file.exists() and "subtitles=filename=assembly.ass" in " ".join(command):
                subtitle_texts.append(subtitle_file.read_text(encoding="utf-8"))
            Path(command[-1]).write_bytes(b"v" * 2048)

        run_ffmpeg.side_effect = create_output
        render_project_assembly(project.pk)

        project.refresh_from_db()
        self.assertEqual(project.status, NarrationProject.Status.READY)
        self.assertAlmostEqual(project.duration_seconds, 1.0)
        self.assertTrue(project.video_file)
        self.assertEqual(len(commands), 3)
        self.assertIn(visual.image.path, commands[0])
        self.assertNotIn(ignored.image.path, commands[0])
        self.assertIn("-an", commands[0])
        self.assertEqual(commands[1][commands[1].index("-c") + 1], "copy")
        self.assertIn(project.audio_asset.audio_file.path, commands[2])
        self.assertEqual(commands[2][commands[2].index("-c:v") + 1], "libx264")
        self.assertIn("subtitles=filename=assembly.ass", commands[2][commands[2].index("-vf") + 1])
        self.assertEqual(len(subtitle_texts), 1)
        self.assertIn("bonjour", subtitle_texts[0])
        self.assertIn("studio", subtitle_texts[0])
        self.assertIn("0:00:00.00", subtitle_texts[0])
        self.assertIn("\\c&H000B9EF5&", subtitle_texts[0])

    @patch("studio.video_services._ffmpeg_path", return_value="ffmpeg")
    @patch("studio.video_services._video_encoder", return_value="libx264")
    @patch("studio.video_services._run_ffmpeg")
    def test_assembly_renders_then_mixes_music_without_reencoding_video(self, run_ffmpeg, _encoder, _ffmpeg):
        from .video_services import render_project_assembly

        project, _visual, _ignored = self.make_assembly_project(with_music=True)

        def create_output(command, *_args, **_kwargs):
            Path(command[-1]).write_bytes(b"v" * 2048)

        run_ffmpeg.side_effect = create_output
        render_project_assembly(project.pk)

        project.refresh_from_db()
        self.assertEqual(project.status, NarrationProject.Status.READY)
        self.assertEqual(run_ffmpeg.call_count, 4)
        first_command = run_ffmpeg.call_args_list[0].args[0]
        music_command = run_ffmpeg.call_args_list[3].args[0]
        self.assertNotIn("-stream_loop", first_command)
        self.assertIn("-an", first_command)
        self.assertEqual(music_command[music_command.index("-c:v") + 1], "copy")
        self.assertEqual(music_command[music_command.index("-c:a") + 1], "aac")

    @patch("studio.video_services._ffmpeg_path", return_value="ffmpeg")
    @patch("studio.video_services._run_ffmpeg")
    def test_invalid_assembly_fails_before_invoking_ffmpeg(self, run_ffmpeg, _ffmpeg):
        from .video_services import render_project_assembly

        project, visual, _ignored = self.make_assembly_project()
        visual.image.delete(save=False)
        NarrationSegment.objects.filter(pk=visual.pk).update(image="")

        render_project_assembly(project.pk)

        run_ffmpeg.assert_not_called()
        project.refresh_from_db()
        self.assertEqual(project.status, NarrationProject.Status.ERROR)
        self.assertIn("Il manque 1 visuel pour les 1 scènes.", project.error_message)


class BackgroundMusicMixTests(MediaTestCase):
    def test_music_mix_uses_looped_input_fades_volume_and_video_copy(self):
        from .video_services import _music_mix_command

        command = _music_mix_command(
            "ffmpeg", "/tmp/final.mp4", "/tmp/theme.mp3", "/tmp/mixed.mp4", 20, 10.0,
        )
        self.assertLess(command.index("-stream_loop"), command.index("-i", command.index("-stream_loop")))
        self.assertEqual(command[command.index("-stream_loop") + 1], "-1")
        self.assertIn("volume=0.200", command[command.index("-filter_complex") + 1])
        self.assertIn("afade=t=in:st=0:d=1.500", command[command.index("-filter_complex") + 1])
        self.assertIn("afade=t=out:st=8.500:d=1.500", command[command.index("-filter_complex") + 1])
        self.assertEqual(command[command.index("-c:v") + 1], "copy")
        self.assertEqual(command[command.index("-c:a") + 1], "aac")
        self.assertIn("-shortest", command)

    @patch("studio.video_services._ffmpeg_path", return_value="ffmpeg")
    @patch("studio.video_services._run_ffmpeg")
    def test_mix_music_with_video_runs_audio_only_final_pass(self, run_ffmpeg, _ffmpeg):
        from .video_services import mix_music_with_video

        with tempfile.TemporaryDirectory(prefix="music-mix-test-") as folder:
            video = Path(folder) / "video.mp4"
            music = Path(folder) / "music.mp3"
            output = Path(folder) / "mixed.mp4"
            video.write_bytes(b"video input")
            music.write_bytes(b"music input")

            def produce_output(command, *_args, **_kwargs):
                Path(command[-1]).write_bytes(b"x" * 2048)

            run_ffmpeg.side_effect = produce_output
            result = mix_music_with_video(video, music, 25, 4.0, output)

        self.assertEqual(result, output)
        command = run_ffmpeg.call_args.args[0]
        self.assertEqual(command[command.index("-c:v") + 1], "copy")
        self.assertEqual(command[command.index("-c:a") + 1], "aac")

    def test_create_add_edit_delete_and_reorder_segments(self):
        response = self.client.post("/projects/create/", {"title": "Mon message"})
        project = NarrationProject.objects.get(title="Mon message")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.client.get("/").status_code, 200)
        self.assertEqual(self.client.get(f"/projects/{project.pk}/").status_code, 200)
        first = project.segments.get(position=0)
        self.client.post(f"/projects/{project.pk}/segments/{first.pk}/save/", {"text": "Bonjour tout le monde."})
        first.refresh_from_db()
        self.assertEqual(first.text, "Bonjour tout le monde.")
        second_response = self.client.post(f"/projects/{project.pk}/segments/add/")
        self.assertEqual(second_response.status_code, 302)
        second = project.segments.get(position=1)
        reorder = self.client.post(
            f"/projects/{project.pk}/segments/reorder/",
            data=f'{{"segment_ids":[{second.pk},{first.pk}]}}',
            content_type="application/json",
        )
        self.assertEqual(reorder.status_code, 200)
        first.refresh_from_db()
        second.refresh_from_db()
        self.assertEqual((second.position, first.position), (0, 1))
        self.client.post(f"/projects/{project.pk}/segments/{second.pk}/delete/")
        self.assertEqual(project.segments.count(), 1)


class NarrationQueueTests(MediaTestCase):
    def make_queued_project(self, title, voice, queued_at):
        project = NarrationProject.objects.create(user=self.user, 
            title=title, voice=voice, status=NarrationProject.Status.QUEUED, queued_at=queued_at,
        )
        NarrationSegment.objects.create(project=project, position=0, text="Bonjour.")
        return project

    def test_prepare_adds_project_to_persistent_queue_without_starting_a_thread(self):
        voice = self.make_voice()
        project = NarrationProject.objects.create(user=self.user, title="À préparer", voice=voice)
        NarrationSegment.objects.create(project=project, position=0, text="Bonjour.")

        response = self.client.post(f"/projects/{project.pk}/prepare/", {"voice_id": voice.pk})

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["ok"])
        project.refresh_from_db()
        self.assertEqual(project.status, NarrationProject.Status.QUEUED)
        self.assertIsNotNone(project.queued_at)
        self.assertIsNone(project.started_at)

    def test_queue_status_reports_fifo_position_and_edits_are_locked(self):
        voice = self.make_voice()
        first = self.make_queued_project("Premier", voice, timezone.now())
        second = self.make_queued_project("Deuxième", voice, timezone.now() + timedelta(seconds=1))

        response = self.client.get(f"/projects/{second.pk}/status/")
        self.assertEqual(response.json()["queue_position"], 2)
        self.assertEqual(response.json()["queue_count"], 2)

        segment = second.segments.get()
        edit = self.client.post(f"/projects/{second.pk}/segments/{segment.pk}/save/", {"text": "Autre."})
        self.assertEqual(edit.status_code, 409)
        segment.refresh_from_db()
        self.assertEqual(segment.text, "Bonjour.")
        self.assertEqual(first.status, NarrationProject.Status.QUEUED)

    def test_worker_claims_jobs_in_fifo_order(self):
        from .management.commands.run_narration_worker import Command

        voice = self.make_voice()
        first = self.make_queued_project("Premier", voice, timezone.now())
        second = self.make_queued_project("Deuxième", voice, timezone.now() + timedelta(seconds=1))

        self.assertEqual(Command._claim_next_job(), first.pk)
        first.refresh_from_db()
        self.assertEqual(first.status, NarrationProject.Status.PROCESSING)
        self.assertIsNone(first.queued_at)
        self.assertEqual(Command._claim_next_job(), second.pk)


class VideoSceneTests(MediaTestCase):
    @staticmethod
    def png_bytes(color=(240, 120, 20)):
        buffer = BytesIO()
        Image.new("RGB", (80, 60), color).save(buffer, format="PNG")
        return buffer.getvalue()

    def test_camera_motion_presets_keep_the_image_aspect_and_create_movement(self):
        from .video_services import _movement_filters

        for motion in ("zoom_in", "zoom_out", "pan_h", "pan_v", "zoom_pan"):
            filters = _movement_filters(motion, 720, 1280, 10)
            self.assertIn("crop=720:1280", filters)
            self.assertTrue(any(item.startswith("zoompan=") for item in filters), motion)
        fixed = _movement_filters("still", 720, 1280, 10)
        self.assertIn("force_original_aspect_ratio=decrease", fixed[0])
        self.assertTrue(any(item.startswith("pad=") for item in fixed))

    def test_vertical_dimensions_and_video_encoder_fallback(self):
        from .video_services import (
            _dimensions, _software_fallback_command, _video_encoder_args,
        )

        self.assertEqual(_dimensions(NarrationProject.VideoFormat.VERTICAL), (1080, 1920))
        self.assertEqual(_dimensions(NarrationProject.VideoFormat.HORIZONTAL), (1280, 720))
        with patch("studio.video_services._video_encoder", return_value="h264_videotoolbox"):
            self.assertEqual(_video_encoder_args("ffmpeg", 1080, 1920), [
                "-c:v", "h264_videotoolbox", "-b:v", "15M",
            ])
        with patch("studio.video_services._video_encoder", return_value="libx264"):
            self.assertEqual(_video_encoder_args("ffmpeg", 1080, 1920), [
                "-c:v", "libx264", "-preset", "ultrafast", "-crf", "24",
            ])

        command = [
            "ffmpeg", "-vf", "null", "-c:v", "h264_videotoolbox", "-b:v", "15M",
            "-pix_fmt", "yuv420p", "output.mp4",
        ]
        self.assertEqual(_software_fallback_command(command), [
            "ffmpeg", "-vf", "null", "-c:v", "libx264", "-preset", "ultrafast",
            "-crf", "24", "-pix_fmt", "yuv420p", "output.mp4",
        ])

    def test_ass_captions_highlight_words_and_keep_legacy_phrases(self):
        from .video_services import _ass_text

        word_segment = types.SimpleNamespace(subtitle_data=[{
            "start": 0.1,
            "end": 1.0,
            "text": "Choisis une petite action.",
            "words": [
                {"word": "Choisis", "start": 0.1, "end": 0.35},
                {"word": "une", "start": 0.38, "end": 0.48},
                {"word": "petite", "start": 0.5, "end": 0.72},
                {"word": "action.", "start": 0.74, "end": 1.0},
            ],
        }])
        ass = _ass_text(word_segment)
        self.assertIn("PlayResX: 1080", ass)
        self.assertIn("PlayResY: 1920", ass)
        self.assertIn(r"{\c&H000B9EF5&}Choisis{\c&H00FFFFFF&}", ass)
        self.assertIn(r"{\c&H000B9EF5&}une{\c&H00FFFFFF&}", ass)
        dialogue_lines = [line for line in ass.splitlines() if line.startswith("Dialogue:")]
        self.assertEqual(len(dialogue_lines), 4)
        self.assertTrue(all(line.count(r"\N") <= 1 for line in dialogue_lines))
        long_words = [
            {"word": word, "start": index * 0.2, "end": index * 0.2 + 0.15}
            for index, word in enumerate([
                "Extraordinaire", "progressivement", "resolument", "attention",
                "transformation", "magnifique", "aujourdhui",
            ])
        ]
        long_ass = _ass_text(types.SimpleNamespace(subtitle_data=[{
            "start": 0.0, "end": 1.5, "text": " ".join(item["word"] for item in long_words),
            "words": long_words,
        }]))
        long_dialogues = [line for line in long_ass.splitlines() if line.startswith("Dialogue:")]
        self.assertGreater(len(long_dialogues), 1)
        self.assertTrue(all(line.count(r"\N") <= 1 for line in long_dialogues))
        horizontal_ass = _ass_text(word_segment, 1280, 720)
        self.assertIn("PlayResX: 1280", horizontal_ass)
        self.assertIn("PlayResY: 720", horizontal_ass)

        legacy_segment = types.SimpleNamespace(subtitle_data=[{
            "start": 0.2, "end": 1.1, "text": "Une phrase complète.",
        }])
        legacy_ass = _ass_text(legacy_segment)
        self.assertIn("Dialogue: 0,0:00:00.20,0:00:01.10,Default", legacy_ass)
        self.assertIn("Une phrase complète.", legacy_ass)
        self.assertNotIn( r"{\c&H000B9EF5&}", legacy_ass)

    def test_animation_settings_defaults_preserve_existing_rendering(self):
        from .video_services import _movement_filters

        project = NarrationProject.objects.create(user=self.user, title="Animation settings")
        segment = NarrationSegment.objects.create(project=project, position=0)
        self.assertEqual(segment.camera_profile, NarrationSegment.CameraProfile.LINEAR)
        self.assertFalse(segment.light_breathing)

        default_filters = _movement_filters("zoom_in", 720, 1280, 10)
        self.assertFalse(any(item.startswith("rotate=") for item in default_filters))
        self.assertFalse(any(item.startswith("eq=") for item in default_filters))

        organic_filters = _movement_filters(
            "zoom_in", 720, 1280, 10,
            camera_profile=NarrationSegment.CameraProfile.ORGANIC,
            light_breathing=True,
        )
        self.assertTrue(any(item.startswith("rotate=") for item in organic_filters))
        self.assertTrue(any(item.startswith("eq=") for item in organic_filters))
        self.assertTrue(any("cos(PI*" in item for item in organic_filters if item.startswith("zoompan=")))

    def test_visual_settings_invalidate_only_video(self):
        project = NarrationProject.objects.create(user=self.user, title="Visual settings", status=NarrationProject.Status.READY)
        project.full_audio.save("voice.wav", ContentFile(b"audio"), save=True)
        project.subtitles.save("voice.srt", ContentFile(b"1\n"), save=True)
        project.video_file.save("video.mp4", ContentFile(b"video"), save=True)
        segment = NarrationSegment.objects.create(
            project=project, position=0, text="Bonjour.", motion=NarrationSegment.Motion.ZOOM_IN,
        )

        response = self.client.post(
            f"/projects/{project.pk}/segments/{segment.pk}/save/",
            {
                "text": "Bonjour.",
                "motion": segment.motion,
                "camera_profile": NarrationSegment.CameraProfile.ORGANIC,
                "light_breathing": "on",
            },
        )

        self.assertEqual(response.status_code, 302)
        project.refresh_from_db()
        segment.refresh_from_db()
        self.assertEqual(segment.camera_profile, NarrationSegment.CameraProfile.ORGANIC)
        self.assertTrue(segment.light_breathing)
        self.assertTrue(project.full_audio.name)
        self.assertTrue(project.subtitles.name)
        self.assertFalse(project.video_file.name)

    def test_import_image_creates_a_scene_and_reuses_empty_first_segment(self):
        project = NarrationProject.objects.create(user=self.user, title="Images")
        first = NarrationSegment.objects.create(project=project, position=0)
        upload = SimpleUploadedFile("coach.png", self.png_bytes(), content_type="image/png")

        response = self.client.post(f"/projects/{project.pk}/scenes/import/", {"files": [upload]})

        self.assertEqual(response.status_code, 302)
        self.assertEqual(project.segments.count(), 1)
        first.refresh_from_db()
        self.assertEqual(Path(first.image.name).suffix, ".png")
        self.assertTrue(Path(first.image.path).is_file())

    def test_import_pdf_creates_one_scene_per_page(self):
        import pymupdf

        document = pymupdf.open()
        document.new_page(width=120, height=160)
        document.new_page(width=160, height=120)
        pdf_data = document.tobytes()
        document.close()
        project = NarrationProject.objects.create(user=self.user, title="PDF")
        NarrationSegment.objects.create(project=project, position=0)
        upload = SimpleUploadedFile("pages.pdf", pdf_data, content_type="application/pdf")

        response = self.client.post(f"/projects/{project.pk}/scenes/import/", {"files": [upload]})

        self.assertEqual(response.status_code, 302)
        scenes = list(project.segments.order_by("position"))
        self.assertEqual(len(scenes), 2)
        self.assertTrue(all(scene.image.name.endswith(".png") for scene in scenes))
        self.assertTrue(all(Path(scene.image.path).is_file() for scene in scenes))

    def test_prepare_queues_video_when_every_scene_has_an_image(self):
        voice = self.make_voice()
        project = NarrationProject.objects.create(user=self.user, title="Vidéo", voice=voice)
        scene = NarrationSegment.objects.create(project=project, position=0, text="Bonjour.")
        scene.image.save("scene.png", ContentFile(self.png_bytes()), save=True)

        response = self.client.post(f"/projects/{project.pk}/prepare/", {"voice_id": voice.pk})

        self.assertEqual(response.status_code, 200)
        project.refresh_from_db()
        self.assertEqual(project.job_type, NarrationProject.JobType.VIDEO)
        self.assertEqual(project.status, NarrationProject.Status.QUEUED)

    @patch("studio.video_services._ffmpeg_path", return_value="ffmpeg")
    @patch("studio.video_services.subprocess.Popen")
    def test_video_export_assembles_scene_and_caption(self, run_ffmpeg, _ffmpeg):
        from .video_services import render_project_video

        track = BackgroundTrack(user=self.user, name="Fond", duration=10.0)
        track.audio_file.save("music.mp3", ContentFile(b"music"), save=True)
        project = NarrationProject.objects.create(
            user=self.user, title="Vidéo", status=NarrationProject.Status.PROCESSING,
            background_track=track, music_volume=18,
        )
        scene = NarrationSegment.objects.create(
            project=project,
            position=0,
            text="Bonjour.",
            subtitle_data=[{"start": 0.0, "end": 0.8, "text": "Bonjour."}],
            duration_seconds=1.0,
        )
        scene.image.save("scene.png", ContentFile(self.png_bytes()), save=False)
        scene.audio.save("scene.wav", ContentFile(b"audio test data"), save=True)

        def make_output(command, **_kwargs):
            Path(command[-1]).write_bytes(b"x" * 2048)
            return types.SimpleNamespace(
                returncode=0,
                communicate=Mock(return_value=("", "")),
                poll=Mock(return_value=0),
            )

        run_ffmpeg.side_effect = make_output
        render_project_video(project.pk)

        project.refresh_from_db()
        self.assertEqual(project.status, NarrationProject.Status.READY)
        self.assertTrue(project.video_file.name.endswith(".mp4"))
        self.assertTrue(Path(project.video_file.path).is_file())
        self.assertEqual(run_ffmpeg.call_count, 3)
        scene_command = run_ffmpeg.call_args_list[0].args[0]
        filters = scene_command[scene_command.index("-vf") + 1]
        self.assertIn("subtitles=filename=scene-0001.ass", filters)
        self.assertLess(filters.index("vignette="), filters.index("subtitles="))
        self.assertIn("s=1080x1920", filters)
        music_command = run_ffmpeg.call_args_list[-1].args[0]
        self.assertEqual(music_command[music_command.index("-c:v") + 1], "copy")
        self.assertEqual(music_command[music_command.index("-stream_loop") + 1], "-1")


    @patch("studio.video_services._ffmpeg_path", return_value="ffmpeg")
    @patch("studio.video_services._probe_clip", return_value={"duration": 0.8, "probe_seconds": 0.0})
    @patch("studio.video_services.wav_duration", return_value=1.0)
    @patch("studio.video_services.subprocess.Popen")
    def test_video_narration_mixes_music_after_final_concat(self, run_ffmpeg, _duration, _probe, _ffmpeg):
        from .video_services import render_video_narration

        track = BackgroundTrack(user=self.user, name="Fond vidéo", duration=15.0)
        track.audio_file.save("music.ogg", ContentFile(b"music"), save=True)
        project = NarrationProject.objects.create(
            user=self.user, title="Narration vidéo", status=NarrationProject.Status.PROCESSING,
            background_track=track, music_volume=22, subtitles_enabled=False,
        )
        scene = NarrationSegment.objects.create(project=project, position=0, text="Bonjour.")
        scene.video_clip.save("clip.mp4", ContentFile(b"video input"), save=True)
        scene.audio.save("voice.wav", ContentFile(b"voice input"), save=True)
        commands = []

        def make_output(command, **_kwargs):
            commands.append(command)
            Path(command[-1]).write_bytes(b"x" * 2048)
            return types.SimpleNamespace(
                returncode=0,
                communicate=Mock(return_value=("", "")),
                poll=Mock(return_value=0),
            )

        run_ffmpeg.side_effect = make_output
        render_video_narration(project.pk)

        self.assertEqual(len(commands), 3)
        self.assertEqual(commands[-1][commands[-1].index("-c:v") + 1], "copy")
        self.assertEqual(commands[-1][commands[-1].index("-stream_loop") + 1], "-1")
        project.refresh_from_db()
        self.assertEqual(project.status, NarrationProject.Status.READY)
        self.assertTrue(project.video_file)


class CacheAndCaptionTests(MediaTestCase):
    def test_cache_key_changes_with_text_and_voice_sample(self):
        voice = self.make_voice()
        first = cache_key_for("Bonjour.", voice)
        self.assertEqual(first, cache_key_for("Bonjour.", voice))
        self.assertNotEqual(first, cache_key_for("Bonsoir.", voice))
        voice.sample.save("replacement.wav", ContentFile(b"another sample"), save=True)
        self.assertNotEqual(first, cache_key_for("Bonjour.", voice))

    @patch("studio.services.align_audio", return_value=[{"start": 0.0, "end": 0.5, "text": "Bonjour."}])
    @patch("studio.services._synthesize_segment")
    def test_identical_text_and_voice_reuses_audio_cache_across_segments(self, synthesize, _align):
        voice = self.make_voice()
        first = NarrationSegment.objects.create(project=NarrationProject.objects.create(user=self.user, title="Premier"), text="Bonjour.")
        second = NarrationSegment.objects.create(project=NarrationProject.objects.create(user=self.user, title="Second"), text="Bonjour.")
        key = cache_key_for("Bonjour.", voice)

        def write_test_wav(_text, _voice, destination):
            with wave.open(str(destination), "wb") as stream:
                stream.setnchannels(1)
                stream.setsampwidth(2)
                stream.setframerate(24000)
                stream.writeframes(b"\x01\x00" * 12000)

        synthesize.side_effect = write_test_wav
        _prepare_segment(first, voice, key)
        _prepare_segment(second, voice, key)
        synthesize.assert_called_once()
        self.assertTrue(second.audio.name)
        self.assertEqual(second.cache_key, key)
        self.assertAlmostEqual(second.duration_seconds, 0.5)

    def test_word_groups_and_project_srt_offsets(self):
        words = [
            {"word": "Bonjour", "start": 0.1, "end": 0.5},
            {"word": "à", "start": 0.55, "end": 0.65},
            {"word": "tous.", "start": 0.7, "end": 1.0},
        ]
        grouped = _group_words(words)[0]
        self.assertEqual(grouped["text"], "Bonjour à tous.")
        self.assertEqual(grouped["words"], words)
        first = types.SimpleNamespace(
            subtitle_data=[{"start": 0.1, "end": 1.0, "text": "Bonjour à tous."}], duration_seconds=2.0,
        )
        second = types.SimpleNamespace(
            subtitle_data=[{"start": 0.0, "end": 0.8, "text": "À bientôt."}], duration_seconds=1.0,
        )
        content = srt_text([first, second])
        self.assertIn("00:00:00,100 --> 00:00:01,000", content)
        self.assertIn("00:00:02,000 --> 00:00:02,800", content)
        self.assertEqual(content.count(" --> "), 2)
        self.assertEqual(_group_words([]), [])

    def test_segment_srt_download_uses_its_aligned_caption_data(self):
        project = NarrationProject.objects.create(user=self.user, title="SRT")
        segment = NarrationSegment.objects.create(
            project=project,
            position=0,
            text="Une phrase.",
            subtitle_data=[{"start": 0.25, "end": 0.9, "text": "Une phrase."}],
        )
        response = self.client.get(f"/projects/{project.pk}/segments/{segment.pk}/subtitles.srt")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"00:00:00,250 --> 00:00:00,900", response.content)
        self.assertIn(b"Une phrase.", response.content)
        self.assertIn("attachment; filename=\"segment-1.srt\"", response["Content-Disposition"])

    def test_failed_alignment_returns_an_error_and_no_fallback_timestamps(self):
        torch_stub = types.SimpleNamespace(cuda=types.SimpleNamespace(is_available=lambda: False))
        whisper_stub = types.SimpleNamespace(
            load_audio=lambda _path: [0.0] * 16000,
            load_align_model=lambda **_kwargs: (object(), object()),
            align=lambda *_args, **_kwargs: {"word_segments": []},
        )
        with patch.dict(sys.modules, {"torch": torch_stub, "whisperx": whisper_stub}):
            with self.assertRaisesRegex(RuntimeError, "aucun mot aligné"):
                align_audio("unused.wav", "Un texte.")

    def test_whisperx_alignment_model_is_loaded_once_per_process(self):
        torch_stub = types.SimpleNamespace(
            cuda=types.SimpleNamespace(is_available=lambda: False),
        )
        align_model, metadata = object(), object()
        loader = types.SimpleNamespace(
            load_audio=lambda _path: [0.0] * 16000,
            load_align_model=Mock(return_value=(align_model, metadata)),
            align=lambda *_args, **_kwargs: {
                "word_segments": [{"word": "Bonjour.", "start": 0.1, "end": 0.5}],
            },
        )
        with (
            patch.dict(sys.modules, {"torch": torch_stub, "whisperx": loader}),
            patch.object(services, "_ALIGNMENT_MODEL", None),
            patch.object(services, "_ALIGNMENT_METADATA", None),
            patch.object(services, "_ALIGNMENT_DEVICE", None),
            patch.object(services, "_ALIGNMENT_BACKEND", None),
        ):
            align_audio("unused.wav", "Bonjour.")
            align_audio("unused.wav", "Bonjour.")
        self.assertEqual(loader.load_align_model.call_count, 1)

    def test_xtts_speaker_conditioning_is_cached_for_the_same_voice(self):
        voice = self.make_voice()
        conditioning = (object(), object())
        get_conditioning = Mock(return_value=conditioning)
        xtts_model = types.SimpleNamespace(
            get_conditioning_latents=get_conditioning,
            inference=lambda *_args, **_kwargs: None,
        )
        model = types.SimpleNamespace(synthesizer=types.SimpleNamespace(tts_model=xtts_model))
        torch_stub = types.SimpleNamespace(inference_mode=nullcontext)
        with (
            patch.dict(sys.modules, {"torch": torch_stub}),
            patch.object(services, "_SPEAKER_CONDITIONING_CACHE", services.OrderedDict()),
        ):
            first = _get_speaker_conditioning(model, voice)
            second = _get_speaker_conditioning(model, voice)
        self.assertIs(first, conditioning)
        self.assertIs(second, conditioning)
        get_conditioning.assert_called_once_with(audio_path=[voice.sample.path])

    def test_xtts_import_and_generate_method_when_ai_package_is_installed(self):
        if not importlib.util.find_spec("TTS"):
            self.skipTest("XTTS dépendances IA non installées dans cet environnement d’exécution.")
        from TTS.api import TTS
        from TTS.tts.models.xtts import Xtts
        from TTS.tts.layers.xtts.gpt import GPT
        self.assertTrue(callable(getattr(Xtts, "inference", None)))
        self.assertTrue(callable(getattr(GPT, "generate", None)))
        self.assertTrue(callable(TTS))


class WavExportTests(MediaTestCase):
    def test_wav_assembly_produces_a_single_playable_pcm_file(self):
        with tempfile.TemporaryDirectory(prefix="studio-wav-test-") as folder:
            folder = Path(folder)
            paths = [folder / "one.wav", folder / "two.wav"]
            for path in paths:
                with wave.open(str(path), "wb") as stream:
                    stream.setnchannels(1)
                    stream.setsampwidth(2)
                    stream.setframerate(24000)
                    stream.writeframes(b"\x01\x00" * 24000)
            output = folder / "full.wav"
            _assemble_wav(paths, output)
            self.assertAlmostEqual(wav_duration(output), 2.0)
            with wave.open(str(output), "rb") as stream:
                self.assertEqual(stream.getnchannels(), 1)
                self.assertEqual(stream.getframerate(), 24000)


class MultiEffectAnimationTests(MediaTestCase):
    def test_legacy_motion_conversion_keeps_old_effect_combinations(self):
        from .models import legacy_animation_settings

        zoom_pan = legacy_animation_settings("zoom_pan", "organic", True)
        self.assertEqual(zoom_pan["camera_profile"], "organic")
        self.assertTrue(zoom_pan["effects"]["zoom"]["enabled"])
        self.assertTrue(zoom_pan["effects"]["pan"]["enabled"])
        self.assertEqual(zoom_pan["effects"]["pan"]["direction"], "diagonal")
        self.assertTrue(zoom_pan["effects"]["light_breathing"]["enabled"])

        parallax = legacy_animation_settings("parallax")
        self.assertTrue(parallax["effects"]["parallax"]["enabled"])
        self.assertFalse(parallax["effects"]["zoom"]["enabled"])

    def test_animation_filters_combine_camera_light_grading_and_subtitles_in_order(self):
        from .video_services import _animation_filter_chain

        settings = {
            "camera_profile": "organic",
            "effects": {
                "zoom": {"enabled": True, "level": "fast", "direction": "in"},
                "pan": {"enabled": True, "level": "slow", "direction": "horizontal"},
                "parallax": {"enabled": True, "level": "medium"},
                "handheld": {"enabled": True, "level": "slow"},
                "light_breathing": {"enabled": True, "level": "medium"},
                "light_leak": {"enabled": True, "level": "slow"},
            },
        }
        filters = _animation_filter_chain(settings, 1080, 1920, 10, "captions.ass")
        zoom_index = next(i for i, item in enumerate(filters) if item.startswith("zoompan="))
        rotate_index = next(i for i, item in enumerate(filters) if item.startswith("rotate="))
        breathing_index = next(i for i, item in enumerate(filters) if item.startswith("eq="))
        leak_index = next(i for i, item in enumerate(filters) if item.startswith("format=rgb24,geq="))
        grade_index = filters.index("eq=contrast=1.04:saturation=1.05")
        subtitle_index = next(i for i, item in enumerate(filters) if item.startswith("subtitles="))
        self.assertLess(zoom_index, rotate_index)
        self.assertLess(rotate_index, breathing_index)
        self.assertLess(breathing_index, leak_index)
        self.assertLess(leak_index, grade_index)
        self.assertLess(grade_index, subtitle_index)

    def test_animation_settings_post_rejects_unknown_values_and_persists_unchecked_boxes(self):
        import json

        project = NarrationProject.objects.create(user=self.user, title="Effets")
        segment = NarrationSegment.objects.create(project=project, position=0, text="Salut.")
        settings = {
            "version": 1,
            "camera_profile": "linear",
            "effects": {
                "zoom": {"enabled": False, "level": "medium", "direction": "in"},
                "pan": {"enabled": False, "level": "medium", "direction": "horizontal"},
                "parallax": {"enabled": False, "level": "medium"},
                "handheld": {"enabled": False, "level": "medium"},
                "light_breathing": {"enabled": False, "level": "medium"},
                "light_leak": {"enabled": False, "level": "medium"},
            },
        }
        response = self.client.post(
            f"/projects/{project.pk}/segments/{segment.pk}/save/",
            {"text": "Salut.", "animation_settings": json.dumps(settings)},
        )
        self.assertEqual(response.status_code, 302)
        segment.refresh_from_db()
        self.assertFalse(any(effect["enabled"] for effect in segment.animation_settings["effects"].values()))
        self.assertEqual(segment.motion, NarrationSegment.Motion.STILL)

        settings["effects"]["zoom"]["level"] = ["fast"]
        response = self.client.post(
            f"/projects/{project.pk}/segments/{segment.pk}/save/",
            {"text": "Salut.", "animation_settings": json.dumps(settings)},
        )
        self.assertEqual(response.status_code, 400)
        segment.refresh_from_db()
        self.assertFalse(segment.animation_settings["effects"]["zoom"]["enabled"])


class VideoNarrationModeTests(MediaTestCase):

    def test_audio_only_action_queues_audio_job_for_video_clips(self):
        voice = self.make_voice()
        project = NarrationProject.objects.create(user=self.user, title="Audio d'abord", voice=voice)
        self.add_video_segment(project)

        page = self.client.get(f"/projects/{project.pk}/")
        self.assertContains(page, "Pré-générer l'audio seul")
        self.assertContains(page, "Audio à générer")
        self.assertContains(page, 'class="editor-audio-cache pending"')

        response = self.client.post(f"/projects/{project.pk}/prepare/", {
            "voice_id": voice.pk,
            "audio_only": "1",
        })
        self.assertEqual(response.status_code, 200)
        project.refresh_from_db()
        self.assertEqual(project.status, NarrationProject.Status.QUEUED)
        self.assertEqual(project.job_type, NarrationProject.JobType.AUDIO)
        self.assertIn("Pré-génération audio", project.status_message)

    def test_audio_cache_badge_turns_green_when_every_segment_is_current(self):
        from .services import _voice_sample_digest, cache_key_for

        voice = self.make_voice()
        project = NarrationProject.objects.create(user=self.user, title="Audio déjà prêt", voice=voice)
        segment = self.add_video_segment(project)
        segment.audio.save("segment-ready.wav", ContentFile(b"audio bytes"), save=False)
        segment.subtitle_data = [{"start": 0.0, "end": 1.0, "text": "Bonjour"}]
        segment.cache_key = cache_key_for(
            segment.text, voice, sample_digest=_voice_sample_digest(voice),
        )
        segment.save()

        page = self.client.get(f"/projects/{project.pk}/")
        self.assertContains(page, "Audio en cache")
        self.assertContains(page, 'class="editor-audio-cache ready"')

    def test_audio_worker_stops_after_prepare_without_running_video_renderers(self):
        from .management.commands.run_narration_worker import Command

        project = NarrationProject.objects.create(user=self.user, 
            title="Pré-génération seule",
            status=NarrationProject.Status.QUEUED,
            queued_at=timezone.now(),
            job_type=NarrationProject.JobType.AUDIO,
        )

        def prepare_audio(project_id):
            NarrationProject.objects.filter(pk=project_id).update(status=NarrationProject.Status.READY)

        with override_settings(MEDIA_ROOT=Path(self.media_dir.name)):
            with (
                patch("studio.management.commands.run_narration_worker.prepare_project", side_effect=prepare_audio) as prepare,
                patch("studio.management.commands.run_narration_worker.render_video_narration") as video_narration,
                patch("studio.management.commands.run_narration_worker.render_project_video") as render_video,
            ):
                Command().handle(once=True, poll_seconds=0.25)

        prepare.assert_called_once_with(project.pk)
        video_narration.assert_not_called()
        render_video.assert_not_called()

    def test_worker_startup_announces_scene_and_assembly_modes(self):
        from .management.commands.run_narration_worker import Command

        command = Command()
        command.stdout = StringIO()
        with override_settings(MEDIA_ROOT=Path(self.media_dir.name)):
            command.handle(once=True, poll_seconds=0.25)

        self.assertIn("Scènes XTTS, rendus Assemblage directs", command.stdout.getvalue())

    def test_probe_clip_extracts_source_video_metadata(self):
        from .video_services import _probe_clip

        result = Mock(
            returncode=0,
            stdout='{"streams":[{"codec_type":"video","duration":"12.5","width":1920,"height":1080,"codec_name":"h264","avg_frame_rate":"24000/1001"},{"codec_type":"audio"}],"format":{"duration":"12.5"}}',
            stderr="",
        )
        with (
            patch("studio.video_services._ffprobe_path", return_value="/fake/ffprobe"),
            patch("studio.video_services.subprocess.run", return_value=result) as probe,
        ):
            metadata = _probe_clip("source.mp4")

        self.assertIn("width", metadata)
        self.assertIn("height", metadata)
        self.assertIn("codec", metadata)
        self.assertIn("fps", metadata)
        self.assertIn("duration", metadata)
        self.assertEqual(metadata["width"], 1920)
        self.assertEqual(metadata["height"], 1080)
        self.assertEqual(metadata["codec"], "h264")
        self.assertAlmostEqual(metadata["fps"], 24000 / 1001)
        self.assertEqual(metadata["duration"], 12.5)
        self.assertTrue(metadata["has_audio"])
        probe.assert_called_once()

    def add_video_segment(self, project, text="Bonjour, le Shiba arrive."):
        segment = NarrationSegment(project=project, position=0, text=text)
        segment.video_clip.save("heygen.mp4", ContentFile(b"video bytes"), save=True)
        return segment

    def test_prepare_offers_and_queues_video_narration_for_clips_with_text(self):
        voice = self.make_voice()
        project = NarrationProject.objects.create(user=self.user, title="Clip narré", voice=voice)
        segment = self.add_video_segment(project)

        page = self.client.get(f"/projects/{project.pk}/")
        self.assertContains(page, "Générer la narration vidéo XTTS")
        self.assertContains(page, "Assembler avec le son d’origine")

        response = self.client.post(f"/projects/{project.pk}/prepare/", {
            "voice_id": voice.pk, "mode": NarrationProject.JobType.VIDEO_NARRATION,
        })
        self.assertEqual(response.status_code, 200)
        project.refresh_from_db()
        self.assertEqual(project.status, NarrationProject.Status.QUEUED)
        self.assertEqual(project.job_type, NarrationProject.JobType.VIDEO_NARRATION)
        self.assertEqual(segment.text, "Bonjour, le Shiba arrive.")

    def test_classic_assembly_still_queues_assembly_job_for_video_clips(self):
        project = NarrationProject.objects.create(user=self.user, title="Assemblage classique")
        self.add_video_segment(project, text="")

        response = self.client.post(f"/projects/{project.pk}/assemble-videos/")
        self.assertEqual(response.status_code, 200)
        project.refresh_from_db()
        self.assertEqual(project.status, NarrationProject.Status.QUEUED)
        self.assertEqual(project.job_type, NarrationProject.JobType.ASSEMBLY)

    def test_video_narration_command_replaces_source_audio_and_handles_duration(self):
        from .video_services import _video_narration_command

        command = _video_narration_command(
            "ffmpeg", "clip.mp4", "voice.wav", "out.mp4", 1080, 1920,
            video_duration=10.0, voice_duration=12.0, subtitle_file="captions.ass",
        )
        self.assertEqual(command[command.index("-map") + 1], "0:v:0")
        self.assertEqual(command[command.index("-map") + 3], "1:a:0")
        self.assertIn("tpad=stop_mode=clone:stop_duration=2.000", command[command.index("-vf") + 1])
        self.assertIn("subtitles=filename=captions.ass", command[command.index("-vf") + 1])
        self.assertIn("-shortest", command)
        self.assertNotIn("zoompan=", command[command.index("-vf") + 1])
        self.assertNotIn("0:a", command)

        shorter_video_chain = _video_narration_command(
            "ffmpeg", "clip.mp4", "voice.wav", "out.mp4", 1080, 1920,
            video_duration=12.0, voice_duration=10.0,
        )
        self.assertNotIn("tpad=", shorter_video_chain[shorter_video_chain.index("-vf") + 1])

    def test_worker_keeps_classic_assembly_routed_to_existing_renderer(self):
        from .management.commands.run_narration_worker import Command

        project = NarrationProject.objects.create(user=self.user, 
            title="Assemblage worker",
            status=NarrationProject.Status.QUEUED,
            queued_at=timezone.now(),
            job_type=NarrationProject.JobType.ASSEMBLY,
        )
        with override_settings(MEDIA_ROOT=Path(self.media_dir.name)):
            with patch("studio.management.commands.run_narration_worker.render_video_assembly") as assemble:
                with patch("studio.management.commands.run_narration_worker.prepare_project") as prepare:
                    Command().handle(once=True, poll_seconds=0.25)
        prepare.assert_not_called()
        assemble.assert_called_once_with(project.pk)

    def test_worker_runs_xtts_before_rendering_video_narration(self):
        from .management.commands.run_narration_worker import Command

        project = NarrationProject.objects.create(user=self.user, 
            title="Narration vidéo worker",
            status=NarrationProject.Status.QUEUED,
            queued_at=timezone.now(),
            job_type=NarrationProject.JobType.VIDEO_NARRATION,
        )

        def prepare_project(project_id):
            NarrationProject.objects.filter(pk=project_id).update(status=NarrationProject.Status.PROCESSING)

        with override_settings(MEDIA_ROOT=Path(self.media_dir.name)):
            with patch("studio.management.commands.run_narration_worker.prepare_project", side_effect=prepare_project) as prepare:
                with patch("studio.management.commands.run_narration_worker.render_video_narration") as render:
                    Command().handle(once=True, poll_seconds=0.25)
        prepare.assert_called_once_with(project.pk)
        render.assert_called_once_with(project.pk)


class ProjectLifecycleAndNamingTests(MediaTestCase):
    def test_create_project_uses_supplied_title_or_generated_date_title(self):
        response = self.client.post("/projects/create/", {"title": "  Ma nouvelle vidéo  "})
        self.assertEqual(response.status_code, 302)
        named = NarrationProject.objects.get()
        self.assertEqual(named.title, "Ma nouvelle vidéo")

        response = self.client.post("/projects/create/", {"title": "   "})
        self.assertEqual(response.status_code, 302)
        generated = NarrationProject.objects.exclude(pk=named.pk).get()
        self.assertRegex(generated.title, r"^Projet du \d{2}/\d{2}/\d{4} à \d{2}h\d{2}$")

    def test_project_can_be_renamed_and_home_displays_the_saved_title(self):
        project = NarrationProject.objects.create(user=self.user, title="Avant")
        response = self.client.post(
            f"/projects/{project.pk}/rename/",
            {"title": "Après le montage", "next": "/?from=sidebar"},
            follow=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.redirect_chain, [("/?from=sidebar", 302)])
        self.assertContains(response, "Projet renommé.")
        project.refresh_from_db()
        self.assertEqual(project.title, "Après le montage")
        self.assertContains(self.client.get("/"), "Après le montage")

    def test_project_rename_rejects_blank_and_overlong_names(self):
        project = NarrationProject.objects.create(user=self.user, title="Nom conservé")
        cases = [
            ("   ", "Le nom ne peut pas être vide."),
            ("x" * 121, "Le nom est trop long (max 120 caractères)."),
        ]
        for title, expected_message in cases:
            with self.subTest(title_length=len(title)):
                response = self.client.post(
                    f"/projects/{project.pk}/rename/", {"title": title}, follow=True,
                )
                self.assertEqual(response.status_code, 200)
                self.assertContains(response, expected_message)
                project.refresh_from_db()
                self.assertEqual(project.title, "Nom conservé")

    def test_project_cannot_be_renamed_by_another_user(self):
        other_user = get_user_model().objects.create_user(username="rename-private", password="SafePass123!x")
        project = NarrationProject.objects.create(user=other_user, title="Projet privé")

        response = self.client.post(f"/projects/{project.pk}/rename/", {"title": "Intrusion"})

        self.assertEqual(response.status_code, 404)
        project.refresh_from_db()
        self.assertEqual(project.title, "Projet privé")

    def test_sidebar_folder_actions_and_create_control_are_available(self):
        folder = ProjectFolder.objects.create(user=self.user, name="Dossier sidebar")

        response = self.client.get("/")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'data-folder-create')
        self.assertContains(response, 'data-create-folder-url="/folders/create/"')
        self.assertContains(response, 'data-sidebar-folder')
        self.assertContains(response, f'href="/projects/?folder={folder.pk}"')
        self.assertContains(response, "data-folder-menu-toggle")
        self.assertContains(response, "Renommer")
        self.assertContains(response, "Supprimer")


    def test_ffmpeg_process_is_terminated_when_cancellation_is_requested(self):
        import subprocess
        from .job_control import JobCancelled
        from .video_services import _run_ffmpeg_once

        process = Mock()
        process.poll.return_value = None
        process.communicate.side_effect = [subprocess.TimeoutExpired("ffmpeg", 0.5), ("", "")]
        cancel_check = Mock(side_effect=[False, True])

        with patch("studio.video_services.subprocess.Popen", return_value=process):
            with self.assertRaises(JobCancelled):
                _run_ffmpeg_once(["ffmpeg", "-i", "input.mp4"], Path(self.media_dir.name),
                                 "test cancellation", cancel_check=cancel_check)

        process.terminate.assert_called_once()
        self.assertFalse(process.kill.called)

    def test_queued_job_is_cancelled_and_returns_to_draft_immediately(self):
        project = NarrationProject.objects.create(user=self.user, 
            title="À annuler",
            status=NarrationProject.Status.QUEUED,
            queued_at=timezone.now(),
            cancel_requested=True,
        )

        response = self.client.post(f"/projects/{project.pk}/cancel/")
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertFalse(payload["cancel_requested"])
        project.refresh_from_db()
        self.assertEqual(project.status, NarrationProject.Status.DRAFT)
        self.assertIsNone(project.queued_at)
        self.assertFalse(project.cancel_requested)

    def test_processing_cancellation_is_honored_by_worker_and_returns_to_draft(self):
        from .services import prepare_project

        voice = self.make_voice()
        project = NarrationProject.objects.create(user=self.user, 
            title="En cours",
            voice=voice,
            status=NarrationProject.Status.PROCESSING,
        )
        NarrationSegment.objects.create(project=project, position=0, text="Texte à annuler.")

        response = self.client.post(f"/projects/{project.pk}/cancel/")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["cancel_requested"])
        project.refresh_from_db()
        self.assertTrue(project.cancel_requested)

        prepare_project(project.pk)

        project.refresh_from_db()
        self.assertEqual(project.status, NarrationProject.Status.DRAFT)
        self.assertFalse(project.cancel_requested)
        self.assertEqual(project.status_message, "Traitement annulé proprement.")

    def test_delete_project_route_is_post_only_and_removes_project(self):
        project = NarrationProject.objects.create(user=self.user, title="Projet de test")
        editor = self.client.get(f"/projects/{project.pk}/")
        self.assertEqual(editor.status_code, 200)
        self.assertGreaterEqual(editor.content.decode().count('name="confirm" value="1"'), 2)
        response = self.client.get(f"/projects/{project.pk}/delete/")
        self.assertEqual(response.status_code, 405)

        missing_confirmation = self.client.post(f"/projects/{project.pk}/delete/")
        self.assertEqual(missing_confirmation.status_code, 400)
        self.assertContains(missing_confirmation, "Confirmation manquante", status_code=400)
        self.assertTrue(NarrationProject.objects.filter(pk=project.pk).exists())

        response = self.client.post(f"/projects/{project.pk}/delete/", {
            "confirm": "1", "next": f"/projects/{project.pk}/",
        })
        self.assertRedirects(response, "/", fetch_redirect_response=False)
        self.assertFalse(NarrationProject.objects.filter(pk=project.pk).exists())

class ProjectDuplicationTests(MediaTestCase):
    def make_visual(self, segment, filename="visual.png"):
        buffer = BytesIO()
        Image.new("RGB", (32, 32), (80, 120, 180)).save(buffer, format="PNG")
        segment.image.save(filename, ContentFile(buffer.getvalue()), save=True)
        return segment.image.name

    def make_source_project(self, *, mode=NarrationProject.Mode.ASSEMBLY, title="Projet test"):
        voice = self.make_voice()
        folder = ProjectFolder.objects.create(user=self.user, name=f"Dossier de {title}")
        asset = AudioAsset.objects.create(
            user=self.user, name="Narration réutilisable", duration=12.0,
            source=AudioAsset.Source.XTTS,
            timings={"version": 1, "words": [], "segments": [], "scene_boundaries": []},
        )
        asset.audio_file.save("narration.wav", ContentFile(b"narration"), save=True)
        track = BackgroundTrack.objects.create(user=self.user, name="Ambiance", duration=20.0)
        track.audio_file.save("music.mp3", ContentFile(b"music"), save=True)
        project = NarrationProject.objects.create(
            user=self.user, title=title, folder=folder, mode=mode, voice=voice, audio_asset=asset,
            background_track=track, music_volume=37,
            video_format=NarrationProject.VideoFormat.HORIZONTAL, subtitles_enabled=False,
            status=NarrationProject.Status.READY,
            job_type=NarrationProject.JobType.VIDEO,
            status_message="Ancien état", error_message="Ancienne erreur",
            progress_percent=80, current_segment=2, total_segments=3,
            queued_at=timezone.now(), started_at=timezone.now(), finished_at=timezone.now(),
            duration_seconds=18.5,
        )
        project.full_audio.save("full.wav", ContentFile(b"export audio"), save=True)
        project.subtitles.save("subtitles.ass", ContentFile(b"export subtitles"), save=True)
        project.video_file.save("render.mp4", ContentFile(b"export video"), save=True)
        segment = NarrationSegment.objects.create(
            project=project, position=0, text="Même narration.",
            motion=NarrationSegment.Motion.PAN_H,
            camera_profile=NarrationSegment.CameraProfile.ORGANIC,
            light_breathing=True,
            animation_settings=default_animation_settings(),
            subtitle_data=[{"text": "ancien"}], duration_seconds=18.5, cache_key="old-cache-key",
        )
        image_name = self.make_visual(segment)
        segment.audio.save("segment.wav", ContentFile(b"generated audio"), save=True)
        return project, segment, image_name

    def duplicate(self, project):
        response = self.client.post(f"/projects/{project.pk}/duplicate/")
        self.assertEqual(response.status_code, 302)
        return NarrationProject.objects.exclude(pk=project.pk).latest("pk")

    def test_assembly_duplicate_copies_settings_and_media_references_but_resets_runtime_state(self):
        source, source_segment, image_name = self.make_source_project()

        response = self.client.post(f"/projects/{source.pk}/duplicate/", follow=True)

        duplicate = response.context["project"]
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.redirect_chain, [(f"/projects/{duplicate.pk}/", 302)])
        self.assertContains(response, "Projet dupliqué. Tu peux maintenant modifier les visuels.")
        self.assertEqual(duplicate.title, "Projet test (copie)")
        self.assertEqual(duplicate.folder_id, source.folder_id)
        self.assertEqual(duplicate.mode, source.mode)
        self.assertEqual(duplicate.voice_id, source.voice_id)
        self.assertEqual(duplicate.audio_asset_id, source.audio_asset_id)
        self.assertEqual(duplicate.background_track_id, source.background_track_id)
        self.assertEqual(duplicate.music_volume, 37)
        self.assertEqual(duplicate.video_format, source.video_format)
        self.assertFalse(duplicate.subtitles_enabled)
        self.assertEqual(duplicate.status, NarrationProject.Status.DRAFT)
        self.assertEqual(duplicate.job_type, NarrationProject.JobType.AUDIO)
        self.assertEqual(duplicate.progress_percent, 0)
        self.assertEqual(duplicate.current_segment, 0)
        self.assertEqual(duplicate.total_segments, 0)
        self.assertEqual(duplicate.status_message, "")
        self.assertEqual(duplicate.error_message, "")
        self.assertFalse(duplicate.queued_at)
        self.assertFalse(duplicate.started_at)
        self.assertFalse(duplicate.finished_at)
        self.assertFalse(duplicate.cancel_requested)
        self.assertFalse(duplicate.full_audio)
        self.assertFalse(duplicate.subtitles)
        self.assertFalse(duplicate.video_file)
        self.assertEqual(duplicate.duration_seconds, 0)

        copied_segment = duplicate.segments.get()
        self.assertNotEqual(copied_segment.pk, source_segment.pk)
        self.assertEqual(copied_segment.position, source_segment.position)
        self.assertEqual(copied_segment.text, source_segment.text)
        self.assertEqual(copied_segment.image.name, image_name)
        self.assertEqual(copied_segment.video_clip.name, source_segment.video_clip.name)
        self.assertEqual(copied_segment.motion, source_segment.motion)
        self.assertEqual(copied_segment.camera_profile, source_segment.camera_profile)
        self.assertEqual(copied_segment.light_breathing, source_segment.light_breathing)
        self.assertEqual(copied_segment.animation_settings, source_segment.animation_settings)
        self.assertFalse(copied_segment.audio)
        self.assertEqual(copied_segment.subtitle_data, [])
        self.assertEqual(copied_segment.cache_key, "")
        self.assertEqual(copied_segment.duration_seconds, 0)

        detail = self.client.get(f"/projects/{duplicate.pk}/")
        self.assertEqual(detail.context["projects"][0].pk, duplicate.pk)

    def test_scene_duplicate_reuses_the_persistent_xtts_cache(self):
        source, source_segment, _image_name = self.make_source_project(mode=NarrationProject.Mode.SCENES)
        source_segment.text = "Texte identique pour le cache partagé."
        source_segment.save(update_fields=["text", "updated_at"])
        duplicate = self.duplicate(source)
        copied_segment = duplicate.segments.get()
        expected_key = cache_key_for(source_segment.text, source.voice)
        cache_audio, cache_metadata = services._persistent_cache_paths(expected_key)
        cache_audio.write_bytes(b"cached wav bytes" * 200)
        cache_metadata.write_text(
            json.dumps({"captions": [{"text": source_segment.text, "start": 0.0, "end": 1.0}], "duration": 1.0}),
            encoding="utf-8",
        )

        with patch("studio.services._synthesize_segment") as synthesize, patch("studio.services.align_audio") as align:
            _captions, _duration, cached = services._prepare_segment(copied_segment, source.voice, expected_key)

        self.assertTrue(cached)
        synthesize.assert_not_called()
        align.assert_not_called()
        copied_segment.refresh_from_db()
        self.assertEqual(copied_segment.cache_key, expected_key)
        self.assertTrue(copied_segment.audio)

    def test_user_cannot_duplicate_another_users_project(self):
        other_user = get_user_model().objects.create_user(username="private-project-owner", password="SafePass123!x")
        project = NarrationProject.objects.create(user=other_user, title="Projet privé")

        response = self.client.post(f"/projects/{project.pk}/duplicate/")

        self.assertEqual(response.status_code, 404)
        self.assertEqual(NarrationProject.objects.filter(user=self.user).count(), 0)
        self.assertTrue(NarrationProject.objects.filter(pk=project.pk).exists())

    def test_duplicate_creation_rolls_back_project_and_partial_segments_on_error(self):
        source, _segment, _image_name = self.make_source_project()
        NarrationSegment.objects.create(project=source, position=1, text="Deuxième segment")
        manager = NarrationSegment.objects
        original_create = manager.create
        call_count = 0

        def fail_on_second_segment(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 2:
                raise RuntimeError("simulated segment failure")
            return original_create(*args, **kwargs)

        old_raise_request_exception = self.client.raise_request_exception
        self.client.raise_request_exception = False
        try:
            with patch.object(manager, "create", side_effect=fail_on_second_segment):
                response = self.client.post(f"/projects/{source.pk}/duplicate/")
        finally:
            self.client.raise_request_exception = old_raise_request_exception

        self.assertEqual(response.status_code, 500)
        self.assertEqual(NarrationProject.objects.count(), 1)
        self.assertEqual(NarrationSegment.objects.filter(project=source).count(), 2)

    def test_duplicate_name_uses_incrementing_copy_suffix(self):
        source, _segment, _image_name = self.make_source_project(title="Projet 26")
        NarrationProject.objects.create(user=self.user, title="Projet 26 (copie)")

        duplicate = self.duplicate(source)

        self.assertEqual(duplicate.title, "Projet 26 (copie 2)")

    def test_video_file_is_referenced_by_the_copy_without_a_second_physical_copy(self):
        source = NarrationProject.objects.create(user=self.user, title="Projet vidéo")
        segment = NarrationSegment.objects.create(project=source, position=0)
        segment.video_clip.save("clip.mp4", ContentFile(b"video bytes"), save=True)
        video_name = segment.video_clip.name

        duplicate = self.duplicate(source)

        copied_segment = duplicate.segments.get()
        self.assertNotEqual(copied_segment.pk, segment.pk)
        self.assertEqual(copied_segment.video_clip.name, video_name)
        self.assertTrue(copied_segment.video_clip.storage.exists(video_name))

    def test_replacing_copy_visual_does_not_change_or_remove_original_file(self):
        source, source_segment, original_image_name = self.make_source_project()
        duplicate = self.duplicate(source)
        copied_segment = duplicate.segments.get()
        uploaded = ProjectAssemblyRenderTests.png_bytes((210, 40, 70))

        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(f"/projects/{duplicate.pk}/assembly/visual/", {
                "position": "0", "visual_type": "image",
                "visual_file": SimpleUploadedFile("replacement.png", uploaded, content_type="image/png"),
            })

        self.assertEqual(response.status_code, 302)
        source_segment.refresh_from_db()
        copied_segment.refresh_from_db()
        self.assertEqual(source_segment.image.name, original_image_name)
        self.assertTrue(source_segment.image.storage.exists(original_image_name))
        self.assertNotEqual(copied_segment.image.name, original_image_name)
        self.assertTrue(copied_segment.image.storage.exists(copied_segment.image.name))

    def test_deleting_duplicate_preserves_media_still_used_by_original(self):
        source, source_segment, image_name = self.make_source_project()
        duplicate = self.duplicate(source)
        copied_segment = duplicate.segments.get()
        from .views import _safe_delete_media_file

        with patch("studio.views._safe_delete_media_file", wraps=_safe_delete_media_file) as safe_delete:
            with self.captureOnCommitCallbacks(execute=True):
                response = self.client.post(f"/projects/{duplicate.pk}/delete/", {"confirm": "1"})

        self.assertEqual(response.status_code, 302)
        self.assertFalse(NarrationProject.objects.filter(pk=duplicate.pk).exists())
        self.assertTrue(any(call.args[0][1] == image_name for call in safe_delete.call_args_list))
        source_segment.refresh_from_db()
        self.assertEqual(source_segment.image.name, image_name)
        self.assertTrue(source_segment.image.storage.exists(image_name))
        self.assertFalse(NarrationSegment.objects.filter(pk=copied_segment.pk).exists())

    def test_queued_or_processing_source_cannot_be_duplicated(self):
        for status in (NarrationProject.Status.QUEUED, NarrationProject.Status.PROCESSING):
            with self.subTest(status=status):
                project = NarrationProject.objects.create(user=self.user, title=f"{status} source", status=status)
                project_count = NarrationProject.objects.filter(user=self.user).count()
                response = self.client.post(f"/projects/{project.pk}/duplicate/", follow=True)
                self.assertEqual(response.status_code, 200)
                self.assertContains(response, "Attends la fin du traitement avant de dupliquer ce projet.")
                self.assertEqual(NarrationProject.objects.filter(user=self.user).count(), project_count)


class QueueSupervisionTests(MediaTestCase):
    def test_queue_page_and_json_api_sort_waiting_projects_and_show_processing(self):
        now = timezone.now()
        later = NarrationProject.objects.create(user=self.user, 
            title="En attente plus tard",
            status=NarrationProject.Status.QUEUED,
            queued_at=now,
        )
        earlier = NarrationProject.objects.create(user=self.user, 
            title="En attente en premier",
            status=NarrationProject.Status.QUEUED,
            queued_at=now - timedelta(minutes=2),
        )
        processing = NarrationProject.objects.create(user=self.user, 
            title="Projet en cours",
            status=NarrationProject.Status.PROCESSING,
            queued_at=now - timedelta(minutes=1),
            progress_percent=45,
            current_segment=2,
            total_segments=5,
        )
        NarrationProject.objects.create(user=self.user, title="Brouillon hors file")

        response = self.client.get("/queue/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            [project.pk for project in response.context["waiting_projects"]],
            [earlier.pk, later.pk],
        )
        self.assertEqual(
            [project.pk for project in response.context["processing_projects"]],
            [processing.pk],
        )
        self.assertEqual(response.context["queue_count"], 2)
        self.assertContains(response, "Projet en cours")
        self.assertContains(response, "Segment 2/5")

        api_response = self.client.get("/queue/status/")
        self.assertEqual(api_response.status_code, 200)
        payload = api_response.json()
        self.assertEqual([project["id"] for project in payload["waiting"]], [earlier.pk, later.pk])
        self.assertEqual([project["id"] for project in payload["processing"]], [processing.pk])
        self.assertEqual(payload["queue_count"], 2)
        self.assertFalse(payload["is_empty"])

    def test_sidebar_queue_count_is_available_globally_and_excludes_processing(self):
        NarrationProject.objects.create(user=self.user, title="Attente 1", status=NarrationProject.Status.QUEUED)
        NarrationProject.objects.create(user=self.user, title="Attente 2", status=NarrationProject.Status.QUEUED)
        NarrationProject.objects.create(user=self.user, title="Traitement actif", status=NarrationProject.Status.PROCESSING)
        NarrationProject.objects.create(user=self.user, title="Brouillon")

        home_response = self.client.get("/")
        self.assertEqual(home_response.status_code, 200)
        self.assertEqual(home_response.context["queue_count"], 2)
        self.assertContains(home_response, 'id="queueCountBadge" aria-label="2 projet(s) en attente">2</span>')

        queue_response = self.client.get("/queue/")
        self.assertEqual(queue_response.status_code, 200)
        self.assertEqual(queue_response.context["queue_count"], 2)

    def test_queue_page_shows_empty_state_when_no_active_jobs(self):
        response = self.client.get("/queue/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "La file est vide.")
        self.assertNotIn('id="queueEmpty" hidden', response.content.decode())
        api_response = self.client.get("/queue/status/")
        self.assertTrue(api_response.json()["is_empty"])




class AuthenticationIsolationTests(MediaTestCase):
    def test_anonymous_visitors_are_redirected_to_login(self):
        self.client.logout()

        response = self.client.get("/")

        self.assertEqual(response.status_code, 302)
        self.assertTrue(response["Location"].startswith("/login/?next=/"))

    def test_project_status_returns_json_401_for_anonymous_visitors(self):
        project = NarrationProject.objects.create(user=self.user, title="Privé")
        self.client.logout()

        response = self.client.get(f"/projects/{project.pk}/status/")

        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json(), {"error": "auth_required"})

    def test_user_cannot_view_or_delete_another_users_project(self):
        other = get_user_model().objects.create_user(
            username="other-owner", password="OtherSafePass123!x",
        )
        project = NarrationProject.objects.create(user=other, title="Projet privé")

        detail = self.client.get(f"/projects/{project.pk}/")
        deletion = self.client.post(f"/projects/{project.pk}/delete/", {"confirm": "1"})

        self.assertEqual(detail.status_code, 404)
        self.assertEqual(deletion.status_code, 404)
        self.assertTrue(NarrationProject.objects.filter(pk=project.pk, user=other).exists())

    def test_user_cannot_use_or_list_another_users_voice(self):
        other = get_user_model().objects.create_user(
            username="voice-owner", password="VoiceSafePass123!x",
        )
        project = NarrationProject.objects.create(user=self.user, title="Mon projet")
        voice = Voice.objects.create(
            user=other, name="Voix privée", category=Voice.Category.OTHER,
            sample="voices/private.wav", consent_confirmed=True,
        )

        page = self.client.get("/")
        response = self.client.post(
            f"/projects/{project.pk}/voice/", {"voice_id": voice.pk},
        )

        self.assertNotContains(page, "Voix privée")
        self.assertEqual(response.status_code, 404)
        project.refresh_from_db()
        self.assertIsNone(project.voice_id)

    def test_registration_creates_and_logs_in_new_user(self):
        self.client.logout()

        response = self.client.post("/register/", {
            "username": "new-user",
            "password1": "LongSecurePass123!x",
            "password2": "LongSecurePass123!x",
        })

        self.assertEqual(response.status_code, 302)
        self.assertTrue(get_user_model().objects.filter(username="new-user").exists())
        self.assertEqual(self.client.get("/").status_code, 200)


class PadSequenceSaveTests(MediaTestCase):
    def make_session(self, user=None):
        session = AudioSession(user=user or self.user, source_kind=AudioSession.SourceKind.UPLOAD)
        session.source_file.save("pad-source.wav", ContentFile(b"source audio"), save=True)
        return session

    @staticmethod
    def valid_payload():
        return {
            "name": "Composition test",
            "sequence_data": {
                "version": 1,
                "recording_duration_ms": 1800,
                "bpm": 120,
                "events": [
                    {"pad_index": 0, "timestamp_ms": 200},
                    {"pad_index": 3, "timestamp_ms": 800},
                ],
            },
            "pad_boundaries": [[index * 0.5, (index + 1) * 0.5] for index in range(16)],
        }

    @patch("studio.views.render_pad_sequence")
    def test_save_sequence_renders_and_creates_background_track(self, render):
        session = self.make_session()

        def fake_render(_source, sequence, boundaries, output_path):
            self.assertEqual(sequence["events"][1], {"pad_index": 3, "timestamp_ms": 800})
            self.assertEqual(sequence["bpm"], 120)
            self.assertEqual(len(boundaries), 16)
            output_path.write_bytes(b"test m4a output")
            return output_path, 1.8

        render.side_effect = fake_render
        response = self.client.post(
            f"/audio-studio/{session.pk}/save-sequence/",
            data=json.dumps(self.valid_payload()),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"redirect_url": "/musics/"})
        track = BackgroundTrack.objects.get(user=self.user, name="Composition test")
        self.assertEqual(track.duration, 1.8)
        self.assertTrue(track.audio_file.name.startswith(f"musics/user_{self.user.pk}/"))
        self.assertTrue(Path(track.audio_file.path).exists())
        self.assertFalse(AudioSession.objects.filter(pk=session.pk).exists())
        render.assert_called_once()

    @patch("studio.views.render_pad_sequence")
    def test_save_sequence_rejects_invalid_pad_without_rendering(self, render):
        session = self.make_session()
        payload = self.valid_payload()
        payload["sequence_data"]["events"][0]["pad_index"] = 16

        response = self.client.post(
            f"/audio-studio/{session.pk}/save-sequence/",
            data=json.dumps(payload),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("indice de pad", response.json()["error"])
        render.assert_not_called()
        self.assertFalse(BackgroundTrack.objects.exists())
        self.assertTrue(AudioSession.objects.filter(pk=session.pk).exists())

    @patch("studio.views.render_pad_sequence")
    def test_save_sequence_rejects_bpm_outside_supported_range(self, render):
        session = self.make_session()
        payload = self.valid_payload()
        payload["sequence_data"]["bpm"] = 201

        response = self.client.post(
            f"/audio-studio/{session.pk}/save-sequence/",
            data=json.dumps(payload),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("BPM", response.json()["error"])
        render.assert_not_called()

    @patch("studio.views.render_pad_sequence")
    def test_save_sequence_rejects_more_than_500_events(self, render):
        session = self.make_session()
        payload = self.valid_payload()
        payload["sequence_data"]["events"] = [
            {"pad_index": 0, "timestamp_ms": 0} for _ in range(501)
        ]

        response = self.client.post(
            f"/audio-studio/{session.pk}/save-sequence/",
            data=json.dumps(payload),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 400)
        render.assert_not_called()
        self.assertFalse(BackgroundTrack.objects.exists())

    def test_user_cannot_save_sequence_for_another_users_session(self):
        other = get_user_model().objects.create_user(
            username="pad-sequence-owner", password="PadSequencePass123!x",
        )
        session = self.make_session(other)

        response = self.client.post(
            f"/audio-studio/{session.pk}/save-sequence/",
            data=json.dumps(self.valid_payload()),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 404)
        self.assertTrue(AudioSession.objects.filter(pk=session.pk, user=other).exists())

    def test_editor_exposes_sequencer_controls_and_save_endpoint(self):
        session = self.make_session()

        response = self.client.get(f"/audio-studio/{session.pk}/")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "data-sequencer-record")
        self.assertContains(response, "data-sequencer-stop")
        self.assertContains(response, "data-sequencer-play")
        self.assertContains(response, "data-sequencer-save")
        self.assertContains(response, f"/audio-studio/{session.pk}/save-sequence/")
