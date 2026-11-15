import tempfile
from datetime import timedelta
from pathlib import Path
from unittest.mock import Mock, patch

from django.contrib.auth import get_user_model
from django.core.files.base import ContentFile
from django.test import TestCase, override_settings
from django.utils import timezone

from . import audio_services
from .models import AudioSession


class AudioEffectServiceTests(TestCase):
    def setUp(self):
        filters = set()
        for required in audio_services.EFFECT_FILTERS.values():
            filters.update(required)
        for required in audio_services.PRESET_FILTERS.values():
            filters.update(required)
        self.available_filters = patch.object(audio_services, "AVAILABLE_FILTERS", frozenset(filters))
        self.available_filters.start()
        self.addCleanup(self.available_filters.stop)

    def test_catalog_contains_twenty_five_effects_and_six_presets(self):
        self.assertEqual(len(audio_services.EFFECT_LABELS), 25)
        self.assertEqual(len(audio_services.PRESET_LABELS), 6)

    def test_ffmpeg_filter_listing_parser_extracts_filter_names(self):
        parsed = audio_services._parse_filter_names(
            " ..C aecho A->A Add echoing\n TSC atempo A->A Adjust tempo\n"
        )
        self.assertEqual(parsed, {"aecho", "atempo"})

    def test_every_static_effect_builds_a_filter_chain(self):
        for name in audio_services.EFFECT_LABELS:
            with self.subTest(effect=name):
                params = {"name": name}
                if name in {"tempo", "pitch"}:
                    params["direction"] = "up"
                chain, duration = audio_services.build_filter_chain(
                    [{"type": "effect", "params": params}], 120.0, 48000,
                )
                self.assertTrue(chain)
                self.assertEqual(duration, 120.0)

    def test_every_preset_builds_a_filter_chain(self):
        for name in audio_services.PRESET_LABELS:
            with self.subTest(preset=name):
                chain, _duration = audio_services.build_filter_chain(
                    [{"type": "preset", "params": {"name": name}}], 20.0, 48000,
                )
                self.assertTrue(chain)

    @patch("studio.audio_services._ffmpeg_path", return_value="ffmpeg")
    @patch("studio.audio_services.subprocess.run")
    @patch("studio.audio_services.probe_audio")
    def test_apply_operations_executes_each_effect_and_preset(self, probe, run, _ffmpeg):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "source.wav"
            source.write_bytes(b"source")

            def create_output(command, **_kwargs):
                Path(command[-1]).write_bytes(b"preview")
                return Mock(returncode=0, stderr="")

            run.side_effect = create_output
            for category, names in (("effect", audio_services.EFFECT_LABELS),
                                    ("preset", audio_services.PRESET_LABELS)):
                for name in names:
                    with self.subTest(category=category, name=name):
                        params = {"name": name}
                        if name in {"tempo", "pitch"}:
                            params["direction"] = "up"
                        probe.side_effect = [(120.0, 48000), (120.0, 48000)]
                        output = Path(folder) / f"{category}-{name}.m4a"
                        audio_services.apply_operations(
                            source, [{"type": category, "params": params}], output,
                        )
                        command = run.call_args.args[0]
                        self.assertEqual(command[command.index("-c:a") + 1], "aac")
                        self.assertIn("-af", command)

    @patch("studio.audio_services._ffmpeg_path", return_value="ffmpeg")
    @patch("studio.audio_services.subprocess.run")
    @patch("studio.audio_services.probe_audio")
    def test_apply_operations_executes_two_combined_effect_chains(self, probe, run, _ffmpeg):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "source.wav"
            source.write_bytes(b"source")

            def create_output(command, **_kwargs):
                Path(command[-1]).write_bytes(b"preview")
                return Mock(returncode=0, stderr="")

            run.side_effect = create_output
            combinations = [
                [
                    {"type": "trim", "params": {"start": 1.0, "end": 9.0}},
                    {"type": "fade", "params": {"in": 0.5, "out": 0.5}},
                    {"type": "volume", "params": {"percent": 70}},
                    {"type": "effect", "params": {"name": "echo_light"}},
                ],
                [
                    {"type": "preset", "params": {"name": "dreamy"}},
                    {"type": "effect", "params": {"name": "pitch", "direction": "down"}},
                ],
            ]
            for index, operations in enumerate(combinations):
                with self.subTest(combination=index):
                    probe.side_effect = [(10.0, 48000), (10.0, 48000)]
                    output = Path(folder) / f"combined-{index}.m4a"
                    audio_services.apply_operations(source, operations, output)
                    self.assertTrue(output.exists())

    def test_operations_are_applied_in_canonical_order(self):
        operations = [
            {"type": "effect", "params": {"name": "echo_light"}},
            {"type": "volume", "params": {"percent": 75}},
            {"type": "fade", "params": {"in": 1.0, "out": 2.0}},
            {"type": "trim", "params": {"start": 5.0, "end": 25.0}},
        ]
        chain, duration = audio_services.build_filter_chain(operations, 30.0, 48000)

        self.assertLess(chain.index("atrim="), chain.index("afade="))
        self.assertLess(chain.index("afade="), chain.index("volume="))
        self.assertLess(chain.index("volume="), chain.index("aecho="))
        self.assertEqual(duration, 20.0)

    def test_reverse_is_rejected_over_five_minutes(self):
        with self.assertRaisesMessage(ValueError, "Fichier trop long pour l’effet reverse (max 5 min)."):
            audio_services.build_filter_chain(
                [{"type": "effect", "params": {"name": "reverse"}}], 300.01, 48000,
            )

    def test_invalid_operation_and_unavailable_filter_are_rejected(self):
        with self.assertRaisesMessage(ValueError, "Type d’opération audio inconnu."):
            audio_services.build_filter_chain([{"type": "custom", "params": {}}], 10, 48000)
        with patch.object(audio_services, "AVAILABLE_FILTERS", frozenset()):
            self.assertNotIn("flanger", dict(audio_services.available_effects()))
            with self.assertRaisesMessage(ValueError, "Cet effet audio n’est pas disponible"):
                audio_services.build_filter_chain(
                    [{"type": "effect", "params": {"name": "flanger"}}], 10, 48000,
                )

    @patch("studio.audio_services.probe_audio", side_effect=[(10.0, 48000), (8.0, 48000)])
    @patch("studio.audio_services._ffmpeg_path", return_value="ffmpeg")
    @patch("studio.audio_services.subprocess.run")
    def test_apply_operations_encodes_aac_and_returns_probed_duration(self, run, _ffmpeg, _probe):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "source.wav"
            output = Path(folder) / "preview.m4a"
            source.write_bytes(b"source")

            def create_output(command, **kwargs):
                output.write_bytes(b"encoded audio")
                return Mock(returncode=0, stderr="")

            run.side_effect = create_output
            result_path, duration = audio_services.apply_operations(
                source, [{"type": "volume", "params": {"percent": 50}}], output,
            )

            self.assertEqual(result_path, output)
            self.assertEqual(duration, 8.0)
            command = run.call_args.args[0]
            self.assertIn("-af", command)
            self.assertIn("volume=0.500000", command)
            self.assertEqual(command[command.index("-c:a") + 1], "aac")
            self.assertEqual(command[command.index("-b:a") + 1], "192k")
            self.assertGreater(run.call_args.kwargs["timeout"], 0)
            self.assertLessEqual(run.call_args.kwargs["timeout"], 60)

    @patch("studio.audio_services.probe_audio", return_value=(10.0, 48000))
    @patch("studio.audio_services._ffmpeg_path", return_value="ffmpeg")
    @patch("studio.audio_services.subprocess.run", side_effect=__import__("subprocess").TimeoutExpired("ffmpeg", 60))
    def test_apply_operations_cleans_partial_output_after_timeout(self, _run, _ffmpeg, _probe):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "source.wav"
            output = Path(folder) / "preview.m4a"
            source.write_bytes(b"source")
            output.write_bytes(b"partial")

            with self.assertRaisesMessage(TimeoutError, "délai maximal de 60 secondes"):
                audio_services.apply_operations(
                    source, [{"type": "volume", "params": {"percent": 50}}], output,
                )
            self.assertFalse(output.exists())

    @patch("studio.audio_services.probe_audio", return_value=(10.0, 48000))
    def test_empty_operation_list_copies_the_original_without_encoding(self, _probe):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "source.mp3"
            output = Path(folder) / "preview.mp3"
            source.write_bytes(b"original")
            result_path, duration = audio_services.apply_operations(source, [], output)
            self.assertEqual(result_path, output)
            self.assertEqual(duration, 10.0)
            self.assertEqual(output.read_bytes(), b"original")


class AudioSessionCleanupTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username="audio-session-owner", password="pass")
        self.media_dir = tempfile.TemporaryDirectory(prefix="audio-session-tests-")
        self.settings_override = override_settings(MEDIA_ROOT=self.media_dir.name)
        self.settings_override.enable()
        self.addCleanup(self.settings_override.disable)
        self.addCleanup(self.media_dir.cleanup)

    def make_session(self, user, name="source.wav"):
        session = AudioSession(user=user, source_kind=AudioSession.SourceKind.UPLOAD)
        session.source_file.save(name, ContentFile(b"source audio"), save=False)
        session.current_preview_file.save("preview.m4a", ContentFile(b"preview audio"), save=False)
        session.save()
        return session

    def test_cleanup_deletes_only_expired_users_sessions_and_files_after_commit(self):
        from .audio_services import cleanup_expired_audio_sessions

        now = timezone.now()
        expired = self.make_session(self.user)
        active = self.make_session(self.user, "active.wav")
        other_user = get_user_model().objects.create_user(username="audio-session-other", password="pass")
        other_expired = self.make_session(other_user, "other.wav")
        cutoff = now - timedelta(hours=25)
        AudioSession.objects.filter(pk__in=[expired.pk, other_expired.pk]).update(updated_at=cutoff)
        expired_paths = [Path(expired.source_file.path), Path(expired.current_preview_file.path)]

        with self.captureOnCommitCallbacks(execute=True):
            deleted = cleanup_expired_audio_sessions(user=self.user, now=now)

        self.assertEqual(deleted, 1)
        self.assertFalse(AudioSession.objects.filter(pk=expired.pk).exists())
        self.assertTrue(AudioSession.objects.filter(pk=active.pk).exists())
        self.assertTrue(AudioSession.objects.filter(pk=other_expired.pk).exists())
        self.assertTrue(all(not path.exists() for path in expired_paths))
