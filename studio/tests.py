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

from django.contrib.auth import get_user_model
from django.core.files.base import ContentFile
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.utils import timezone
from PIL import Image

from .models import AudioAsset, BackgroundTrack, NarrationProject, NarrationSegment, Voice
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


class AudioAssetModelTests(MediaTestCase):
    def test_audio_asset_uses_user_scoped_uuid_path_and_project_defaults_to_xtts(self):
        project = NarrationProject.objects.create(user=self.user, title="Ancien projet")
        asset = AudioAsset(user=self.user, name="Narration importée", duration=12.5)
        asset.audio_file.save("narration.m4a", ContentFile(b"audio data"), save=True)

        self.assertEqual(project.audio_source, NarrationProject.AudioSource.XTTS)
        self.assertTrue(asset.audio_file.name.startswith(f"audios/user_{self.user.pk}/"))
        self.assertEqual(Path(asset.audio_file.name).suffix, ".m4a")
        self.assertEqual(len(Path(asset.audio_file.name).stem), 32)

    def test_audio_asset_cannot_be_deleted_while_a_project_references_it(self):
        from django.db.models.deletion import ProtectedError

        asset = AudioAsset(user=self.user, name="Narration", duration=12.5)
        asset.audio_file.save("narration.wav", ContentFile(b"audio data"), save=True)
        NarrationProject.objects.create(
            user=self.user, title="Projet protégé", imported_audio=asset,
            audio_source=NarrationProject.AudioSource.IMPORTED,
        )

        with self.assertRaises(ProtectedError):
            asset.delete()

    def test_audio_source_data_migration_sets_every_existing_project_to_xtts(self):
        from django.apps import apps as django_apps
        from importlib import import_module

        projects = [
            NarrationProject.objects.create(user=self.user, title=f"Projet historique {index}")
            for index in range(3)
        ]
        NarrationProject.objects.filter(pk__in=[project.pk for project in projects]).update(audio_source="imported")

        migration = import_module("studio.migrations.0016_audioasset_audio_source")
        migration.set_existing_projects_to_xtts(django_apps, None)

        self.assertFalse(
            NarrationProject.objects.filter(pk__in=[project.pk for project in projects])
            .exclude(audio_source=NarrationProject.AudioSource.XTTS).exists()
        )

class ProjectViewTests(MediaTestCase):
    @patch("studio.services.hash_file", wraps=services.hash_file)
    def test_project_detail_hashes_voice_sample_once_for_multiple_segments(self, hash_sample):
        voice = self.make_voice()
        project = NarrationProject.objects.create(user=self.user, title="Projet", voice=voice)
        NarrationSegment.objects.create(project=project, position=0, text="Premier segment.")
        NarrationSegment.objects.create(project=project, position=1, text="Deuxième segment.")

        response = self.client.get(f"/projects/{project.pk}/")

        self.assertEqual(response.status_code, 200)
        hash_sample.assert_called_once_with(voice.sample.path)

    def test_changing_project_voice_is_persisted_and_invalidates_complete_exports(self):
        project = NarrationProject.objects.create(user=self.user, title="Voix différente")
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

    def test_changing_background_music_invalidates_only_the_video_export(self):
        track = BackgroundTrack(user=self.user, name="Fond doux", duration=30.0)
        track.audio_file.save("music.mp3", ContentFile(b"music bytes"), save=True)
        project = NarrationProject.objects.create(
            user=self.user, title="Cache audio conservé", status=NarrationProject.Status.READY,
        )
        project.full_audio.save("full.wav", ContentFile(b"voice cache"), save=True)
        project.subtitles.save("captions.srt", ContentFile(b"subtitle cache"), save=True)
        project.video_file.save("old.mp4", ContentFile(b"old video"), save=True)
        segment = NarrationSegment.objects.create(
            project=project, position=0, text="Bonjour.", subtitle_data=[{"start": 0, "end": 1}],
            cache_key="voice-cache-key",
        )
        segment.audio.save("segment.wav", ContentFile(b"segment audio"), save=True)
        audio_name = segment.audio.name

        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(f"/projects/{project.pk}/music/", {
                "background_track_id": track.pk, "music_volume": "35",
            })

        self.assertEqual(response.status_code, 302)
        project.refresh_from_db()
        segment.refresh_from_db()
        self.assertEqual(project.background_track_id, track.pk)
        self.assertEqual(project.music_volume, 35)
        self.assertFalse(project.video_file)
        self.assertTrue(project.full_audio)
        self.assertTrue(project.subtitles)
        self.assertEqual(segment.audio.name, audio_name)
        self.assertEqual(segment.cache_key, "voice-cache-key")

    def test_background_music_selection_rejects_other_users_tracks_and_bad_volume(self):
        other_user = get_user_model().objects.create_user(username="music-owner", password="SafePass123!x")
        track = BackgroundTrack(user=other_user, name="Privée", duration=12.0)
        track.audio_file.save("private.ogg", ContentFile(b"private audio"), save=True)
        project = NarrationProject.objects.create(user=self.user, title="Mon projet")

        self.assertEqual(self.client.post(f"/projects/{project.pk}/music/", {
            "background_track_id": track.pk, "music_volume": "20",
        }).status_code, 404)
        self.assertEqual(self.client.post(f"/projects/{project.pk}/music/", {
            "background_track_id": "", "music_volume": "101",
        }).status_code, 400)


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
        response = self.client.post(f"/projects/{project.pk}/rename/", {"title": "Après le montage"})
        self.assertEqual(response.status_code, 302)
        project.refresh_from_db()
        self.assertEqual(project.title, "Après le montage")
        self.assertContains(self.client.get("/"), "Après le montage")


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
        response = self.client.get(f"/projects/{project.pk}/delete/")
        self.assertEqual(response.status_code, 405)

        response = self.client.post(f"/projects/{project.pk}/delete/")
        self.assertEqual(response.status_code, 302)
        self.assertFalse(NarrationProject.objects.filter(pk=project.pk).exists())

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
        deletion = self.client.post(f"/projects/{project.pk}/delete/", {"confirm": "yes"})

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
