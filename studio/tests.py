import importlib.util
from io import BytesIO
import sys
import tempfile
import types
import wave
from contextlib import nullcontext
from datetime import timedelta
from pathlib import Path
from unittest.mock import Mock, patch

from django.core.files.base import ContentFile
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.utils import timezone
from PIL import Image

from .models import NarrationProject, NarrationSegment, Voice
from .services import (
    _assemble_wav, _get_speaker_conditioning, _group_words, _prepare_segment, align_audio,
    cache_key_for, srt_text, wav_duration,
)
from . import services


class MediaTestCase(TestCase):
    def setUp(self):
        self.media_dir = tempfile.TemporaryDirectory(prefix="studio-voix-tests-")
        self.override = override_settings(MEDIA_ROOT=self.media_dir.name)
        self.override.enable()
        self.addCleanup(self.override.disable)
        self.addCleanup(self.media_dir.cleanup)

    def make_voice(self, name="Voix test", category=Voice.Category.OTHER):
        voice = Voice(name=name, category=category, consent_confirmed=True)
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


class ProjectViewTests(MediaTestCase):
    @patch("studio.services.hash_file", wraps=services.hash_file)
    def test_project_detail_hashes_voice_sample_once_for_multiple_segments(self, hash_sample):
        voice = self.make_voice()
        project = NarrationProject.objects.create(title="Projet", voice=voice)
        NarrationSegment.objects.create(project=project, position=0, text="Premier segment.")
        NarrationSegment.objects.create(project=project, position=1, text="Deuxième segment.")

        response = self.client.get(f"/projects/{project.pk}/")

        self.assertEqual(response.status_code, 200)
        hash_sample.assert_called_once_with(voice.sample.path)

    def test_changing_project_voice_is_persisted_and_invalidates_complete_exports(self):
        project = NarrationProject.objects.create(title="Voix différente")
        voice = self.make_voice("Voix B", Voice.Category.WOMAN)
        project.full_audio.save("old.wav", ContentFile(b"old audio"), save=True)
        project.subtitles.save("old.srt", ContentFile(b"old subtitles"), save=True)
        project.status = NarrationProject.Status.READY
        project.save(update_fields=["status"])

        response = self.client.post(f"/projects/{project.pk}/voice/", {"voice_id": voice.pk})
        self.assertEqual(response.status_code, 302)
        project.refresh_from_db()
        self.assertEqual(project.voice_id, voice.pk)
        self.assertEqual(project.status, NarrationProject.Status.DRAFT)
        self.assertFalse(project.full_audio)
        self.assertFalse(project.subtitles)

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
        project = NarrationProject.objects.create(
            title=title, voice=voice, status=NarrationProject.Status.QUEUED, queued_at=queued_at,
        )
        NarrationSegment.objects.create(project=project, position=0, text="Bonjour.")
        return project

    def test_prepare_adds_project_to_persistent_queue_without_starting_a_thread(self):
        voice = self.make_voice()
        project = NarrationProject.objects.create(title="À préparer", voice=voice)
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

    def test_import_image_creates_a_scene_and_reuses_empty_first_segment(self):
        project = NarrationProject.objects.create(title="Images")
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
        project = NarrationProject.objects.create(title="PDF")
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
        project = NarrationProject.objects.create(title="Vidéo", voice=voice)
        scene = NarrationSegment.objects.create(project=project, position=0, text="Bonjour.")
        scene.image.save("scene.png", ContentFile(self.png_bytes()), save=True)

        response = self.client.post(f"/projects/{project.pk}/prepare/", {"voice_id": voice.pk})

        self.assertEqual(response.status_code, 200)
        project.refresh_from_db()
        self.assertEqual(project.job_type, NarrationProject.JobType.VIDEO)
        self.assertEqual(project.status, NarrationProject.Status.QUEUED)

    @patch("studio.video_services._ffmpeg_path", return_value="ffmpeg")
    @patch("studio.video_services.subprocess.run")
    def test_video_export_assembles_scene_and_caption(self, run_ffmpeg, _ffmpeg):
        from .video_services import render_project_video

        project = NarrationProject.objects.create(title="Vidéo", status=NarrationProject.Status.PROCESSING)
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
            return types.SimpleNamespace(returncode=0, stderr="")

        run_ffmpeg.side_effect = make_output
        render_project_video(project.pk)

        project.refresh_from_db()
        self.assertEqual(project.status, NarrationProject.Status.READY)
        self.assertTrue(project.video_file.name.endswith(".mp4"))
        self.assertTrue(Path(project.video_file.path).is_file())
        self.assertEqual(run_ffmpeg.call_count, 2)
        scene_command = run_ffmpeg.call_args_list[0].args[0]
        self.assertIn("subtitles=filename=scene-0001.srt", scene_command[scene_command.index("-vf") + 1])


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
        first = NarrationSegment.objects.create(project=NarrationProject.objects.create(title="Premier"), text="Bonjour.")
        second = NarrationSegment.objects.create(project=NarrationProject.objects.create(title="Second"), text="Bonjour.")
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
        self.assertEqual(_group_words(words)[0]["text"], "Bonjour à tous.")
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
        project = NarrationProject.objects.create(title="SRT")
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
