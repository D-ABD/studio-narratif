"""Local voice, XTTS, forced-alignment, and WAV/SRT export services."""
import hashlib
import json
import logging
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import time
import wave
from collections import OrderedDict

from django.conf import settings
from django.core.files import File
from django.core.files.base import ContentFile
from django.db import close_old_connections
from django.utils import timezone

from .models import NarrationProject, NarrationSegment, Voice

MODEL_ID = "xtts_v2"
LANGUAGE_CODE = "fr"
SAMPLE_RATE = 24000
logger = logging.getLogger(__name__)
_TTS = None
_TTS_INIT_LOCK = threading.Lock()
_TTS_INFER_LOCK = threading.Lock()
_ALIGN_LOCK = threading.Lock()
_ALIGNMENT_MODEL = None
_ALIGNMENT_METADATA = None
_ALIGNMENT_DEVICE = None
_ALIGNMENT_BACKEND = None
_SPEAKER_CONDITIONING_CACHE = OrderedDict()
_SPEAKER_CONDITIONING_LOCK = threading.Lock()
_SPEAKER_CONDITIONING_CACHE_LIMIT = 8
_PIPELINE_LOCK = threading.Lock()


def _ffmpeg_path():
    configured = os.environ.get("FFMPEG_BINARY")
    if configured and Path(configured).is_file():
        return configured

    # Prefer the dedicated Conda environment, which can include libass even
    # when the system/Homebrew FFmpeg build does not.
    candidates = []
    conda_prefix = os.environ.get("CONDA_PREFIX")
    if conda_prefix:
        prefix = Path(conda_prefix).expanduser()
        if prefix.name == "ffmpeg-subs":
            candidates.append(prefix / "bin" / "ffmpeg")
        else:
            candidates.append(prefix / "envs" / "ffmpeg-subs" / "bin" / "ffmpeg")

    conda_exe = os.environ.get("CONDA_EXE")
    if conda_exe:
        conda_base = Path(conda_exe).expanduser().resolve().parent.parent
        candidates.append(conda_base / "envs" / "ffmpeg-subs" / "bin" / "ffmpeg")

    # Common macOS Conda locations also cover launching the app outside an
    # activated Conda shell, while keeping the override above available.
    home = Path.home()
    for conda_base in (
        home / "miniconda3",
        home / "anaconda3",
        home / "Applications" / "miniconda3",
        Path("/opt/miniconda3"),
        Path("/opt/anaconda3"),
    ):
        candidates.append(conda_base / "envs" / "ffmpeg-subs" / "bin" / "ffmpeg")

    seen = set()
    for candidate in candidates:
        candidate = candidate.expanduser()
        if candidate in seen:
            continue
        seen.add(candidate)
        if candidate.is_file():
            return str(candidate)

    path = shutil.which("ffmpeg")
    if not path:
        raise RuntimeError("FFmpeg est introuvable. Installe-le puis relance Petit Studio Voix.")
    return path


def hash_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def normalize_voice_sample(upload):
    """Convert WAV/MP3/browser recordings to local mono 24 kHz PCM WAV."""
    ffmpeg = _ffmpeg_path()
    suffix = Path(getattr(upload, "name", "sample.wav")).suffix.lower() or ".wav"
    with tempfile.TemporaryDirectory(prefix="petit-studio-voice-") as folder:
        source = Path(folder) / f"sample{suffix}"
        output = Path(folder) / "normalized.wav"
        upload.seek(0)
        source.write_bytes(upload.read())
        result = subprocess.run(
            [ffmpeg, "-y", "-v", "error", "-i", str(source), "-ac", "1", "-ar", str(SAMPLE_RATE),
             "-c:a", "pcm_s16le", str(output)],
            capture_output=True, text=True, check=False,
        )
        if result.returncode or not output.exists() or output.stat().st_size < 1000:
            detail = (result.stderr or "").strip().splitlines()[-1:]
            raise RuntimeError("Échantillon audio illisible. Essaie un WAV ou MP3 valide." + (f" ({detail[0]})" if detail else ""))
        return output.read_bytes()


def _voice_sample_digest(voice):
    if not voice.sample or not voice.sample.name:
        raise ValueError("La voix ne possède pas d’échantillon audio.")
    return voice.sample_sha256 or hash_file(voice.sample.path)


def cache_key_for(text, voice, model_id=MODEL_ID, language=LANGUAGE_CODE, sample_digest=None):
    """Stable cache identity: exact text, sample bytes, model, and language."""
    sample_digest = sample_digest or _voice_sample_digest(voice)
    source = json.dumps({
        "text": text.strip(),
        "voice_sample": sample_digest,
        "model": model_id,
        "language": language,
    }, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(source.encode("utf-8")).hexdigest()


def _get_tts():
    global _TTS
    if _TTS is None:
        with _TTS_INIT_LOCK:
            if _TTS is None:
                try:
                    import torch
                    from TTS.api import TTS
                    from TTS.tts.models.xtts import Xtts
                    from TTS.tts.layers.xtts.gpt import GPT
                except ImportError as exc:
                    raise RuntimeError(
                        "XTTS n’est pas installé dans cet environnement. Active .venv et suis l’étape 4 du README."
                    ) from exc
                # In this Coqui release, sentence synthesis is Xtts.inference(); the
                # autoregressive token generation method is GPT.generate().
                if not callable(getattr(Xtts, "inference", None)) or not callable(getattr(GPT, "generate", None)):
                    raise RuntimeError("API XTTS incomplète : Xtts.inference et GPT.generate sont requis.")
                # Avoid large CPU thread pools on small machines. Set this before
                # XTTS/WhisperX starts doing tensor work; users can benchmark 1/2/4.
                try:
                    cpu_threads = int(os.environ.get("STUDIO_TORCH_THREADS", min(4, os.cpu_count() or 1)))
                except (TypeError, ValueError):
                    cpu_threads = min(4, os.cpu_count() or 1)
                if cpu_threads > 0:
                    torch.set_num_threads(cpu_threads)
                    logger.info("PyTorch utilisera %s fil(s) CPU.", cpu_threads)
                _TTS = TTS(MODEL_ID_TO_CHECKPOINT, gpu=torch.cuda.is_available())
    return _TTS


MODEL_ID_TO_CHECKPOINT = "tts_models/multilingual/multi-dataset/xtts_v2"


def _normalize_generated_audio(source, destination):
    result = subprocess.run(
        [_ffmpeg_path(), "-y", "-v", "error", "-i", str(source), "-ac", "1", "-ar", str(SAMPLE_RATE),
         "-c:a", "pcm_s16le", str(destination)],
        capture_output=True, text=True, check=False,
    )
    if result.returncode or not Path(destination).exists():
        detail = (result.stderr or "").strip().splitlines()[-1:]
        raise RuntimeError("Impossible de normaliser la narration." + (f" {detail[0]}" if detail else ""))


def wav_duration(path):
    with wave.open(str(path), "rb") as audio:
        if audio.getnframes() == 0 or audio.getframerate() <= 0:
            raise RuntimeError("Le fichier WAV est vide ou invalide.")
        return audio.getnframes() / audio.getframerate()


def _group_words(words, max_words=7):
    groups, current = [], []
    for item in words:
        word = (item.get("word") or "").strip()
        start, end = item.get("start"), item.get("end")
        if not word or start is None or end is None:
            continue
        current.append({"word": word, "start": float(start), "end": float(end)})
        if len(current) >= max_words or word.endswith((".", "?", "!", ":", ";")):
            groups.append(current)
            current = []
    if current:
        groups.append(current)
    return [
        {"start": group[0]["start"], "end": group[-1]["end"],
         "text": " ".join(item["word"] for item in group)}
        for group in groups if group[-1]["end"] > group[0]["start"]
    ]


def align_audio(audio_path, narration_text):
    """Align authored French text against XTTS output; never synthesize fallback timestamps."""
    try:
        import torch
        import whisperx
    except ImportError as exc:
        raise RuntimeError("WhisperX n’est pas installé. Installe les dépendances de requirements-ai.txt.") from exc
    device = "cuda" if torch.cuda.is_available() else "cpu"
    audio = whisperx.load_audio(str(audio_path))
    duration = len(audio) / 16000.0
    try:
        global _ALIGNMENT_MODEL, _ALIGNMENT_METADATA, _ALIGNMENT_DEVICE, _ALIGNMENT_BACKEND
        with _ALIGN_LOCK:
            # WhisperX's alignment model is large. Keep it in memory for the
            # lifetime of this Django process instead of reloading it per segment.
            if _ALIGNMENT_MODEL is None or _ALIGNMENT_DEVICE != device or _ALIGNMENT_BACKEND is not whisperx:
                _ALIGNMENT_MODEL, _ALIGNMENT_METADATA = whisperx.load_align_model(
                    language_code=LANGUAGE_CODE, device=device,
                )
                _ALIGNMENT_DEVICE = device
                _ALIGNMENT_BACKEND = whisperx
            model, metadata = _ALIGNMENT_MODEL, _ALIGNMENT_METADATA
            result = whisperx.align(
                [{"start": 0.0, "end": duration, "text": narration_text}],
                model, metadata, audio, device, return_char_alignments=False,
            )
        captions = _group_words(result.get("word_segments", []))
    except Exception as exc:
        raise RuntimeError(f"WhisperX n’a pas pu aligner le texte sur l’audio : {exc}") from exc
    if not captions:
        raise RuntimeError("WhisperX n’a renvoyé aucun mot aligné. Le texte et l’audio n’ont pas été exportés.")
    return captions


def _get_speaker_conditioning(model, voice):
    """Reuse XTTS voice latents across segments from the same sample."""
    xtts_model = getattr(getattr(model, "synthesizer", None), "tts_model", None)
    get_latents = getattr(xtts_model, "get_conditioning_latents", None)
    if not callable(get_latents) or not callable(getattr(xtts_model, "inference", None)):
        return None

    sample_digest = voice.sample_sha256 or hash_file(voice.sample.path)
    key = (id(xtts_model), sample_digest)
    with _SPEAKER_CONDITIONING_LOCK:
        if key in _SPEAKER_CONDITIONING_CACHE:
            _SPEAKER_CONDITIONING_CACHE.move_to_end(key)
            return _SPEAKER_CONDITIONING_CACHE[key]

        import torch
        with torch.inference_mode():
            conditioning = get_latents(audio_path=[voice.sample.path])
        _SPEAKER_CONDITIONING_CACHE[key] = conditioning
        while len(_SPEAKER_CONDITIONING_CACHE) > _SPEAKER_CONDITIONING_CACHE_LIMIT:
            _SPEAKER_CONDITIONING_CACHE.popitem(last=False)
        return conditioning


def _synthesize_segment(text, voice, destination):
    model = _get_tts()
    with tempfile.TemporaryDirectory(prefix="petit-studio-xtts-") as folder:
        raw = Path(folder) / "xtts.wav"
        with _TTS_INFER_LOCK:
            try:
                conditioning = _get_speaker_conditioning(model, voice)
                if conditioning is None:
                    model.tts_to_file(
                        text=text, file_path=str(raw), speaker_wav=[voice.sample.path], language=LANGUAGE_CODE,
                        split_sentences=True,
                    )
                else:
                    xtts_model = model.synthesizer.tts_model
                    import torch
                    with torch.inference_mode():
                        output = xtts_model.inference(
                            text, LANGUAGE_CODE, *conditioning, enable_text_splitting=True,
                        )
                    model.synthesizer.save_wav(wav=output["wav"], path=str(raw))
            except Exception as exc:
                raise RuntimeError(f"XTTS n’a pas pu générer ce segment : {exc}") from exc
        if not raw.exists() or raw.stat().st_size < 1000:
            raise RuntimeError("XTTS n’a pas produit de fichier audio pour ce segment.")
        _normalize_generated_audio(raw, destination)


def _segment_is_cached(segment, expected_key):
    if segment.cache_key != expected_key or not segment.audio or not segment.subtitle_data:
        return False
    try:
        return Path(segment.audio.path).is_file() and Path(segment.audio.path).stat().st_size > 1000
    except (OSError, ValueError):
        return False


def _persistent_cache_paths(cache_key):
    cache_dir = Path(settings.MEDIA_ROOT) / "audio-cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    return cache_dir / f"{cache_key}.wav", cache_dir / f"{cache_key}.json"


def _attach_cached_segment(segment, expected_key, audio_path, captions, duration):
    if segment.audio and segment.audio.name:
        segment.audio.delete(save=False)
    with open(audio_path, "rb") as stream:
        segment.audio.save(f"segment-{segment.pk}-{expected_key[:12]}.wav", File(stream), save=False)
    segment.subtitle_data = captions
    segment.duration_seconds = duration
    segment.cache_key = expected_key
    segment.save(update_fields=["audio", "subtitle_data", "duration_seconds", "cache_key", "updated_at"])


def _prepare_segment(segment, voice, expected_key, on_progress=None, timings=None):
    if _segment_is_cached(segment, expected_key):
        if timings is not None:
            timings.update({"cached": True, "xtts_seconds": 0.0, "whisperx_seconds": 0.0})
        return segment.subtitle_data, segment.duration_seconds, True
    cache_audio, cache_metadata = _persistent_cache_paths(expected_key)
    if cache_audio.is_file() and cache_metadata.is_file() and cache_audio.stat().st_size > 1000:
        try:
            metadata = json.loads(cache_metadata.read_text(encoding="utf-8"))
            captions = metadata["captions"]
            duration = float(metadata["duration"])
            if captions and duration > 0:
                _attach_cached_segment(segment, expected_key, cache_audio, captions, duration)
                if timings is not None:
                    timings.update({"cached": True, "xtts_seconds": 0.0, "whisperx_seconds": 0.0})
                return captions, duration, True
        except (OSError, ValueError, KeyError, json.JSONDecodeError, TypeError):
            # A damaged local cache is ignored and rebuilt below.
            pass
    with tempfile.TemporaryDirectory(prefix="petit-studio-segment-") as folder:
        audio_path = Path(folder) / "segment.wav"
        if on_progress:
            on_progress("Synthèse XTTS", 0.2)
        synthesis_started = time.perf_counter()
        _synthesize_segment(segment.text, voice, audio_path)
        synthesis_seconds = time.perf_counter() - synthesis_started
        if on_progress:
            on_progress(f"Alignement WhisperX · XTTS {synthesis_seconds:.1f} s", 0.68)
        alignment_started = time.perf_counter()
        captions = align_audio(audio_path, segment.text)
        alignment_seconds = time.perf_counter() - alignment_started
        duration = wav_duration(audio_path)
        if timings is not None:
            timings.update({
                "cached": False,
                "xtts_seconds": synthesis_seconds,
                "whisperx_seconds": alignment_seconds,
            })
        logger.info(
            "Segment %s préparé : XTTS %.1f s, WhisperX %.1f s.",
            segment.pk, synthesis_seconds, alignment_seconds,
        )
        cache_audio, cache_metadata = _persistent_cache_paths(expected_key)
        temporary_audio = cache_audio.with_suffix(".wav.tmp")
        temporary_metadata = cache_metadata.with_suffix(".json.tmp")
        shutil.copy2(audio_path, temporary_audio)
        temporary_metadata.write_text(
            json.dumps({"captions": captions, "duration": duration}, ensure_ascii=False), encoding="utf-8",
        )
        temporary_audio.replace(cache_audio)
        temporary_metadata.replace(cache_metadata)
        _attach_cached_segment(segment, expected_key, audio_path, captions, duration)
    return captions, duration, False


def _timestamp(seconds):
    milliseconds = max(0, round(seconds * 1000))
    hours, remainder = divmod(milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    whole_seconds, millis = divmod(remainder, 1000)
    return f"{hours:02}:{minutes:02}:{whole_seconds:02},{millis:03}"


def srt_text(segments):
    """Build one SRT with each segment's captions offset into project time."""
    blocks, offset, sequence = [], 0.0, 1
    for segment in segments:
        captions = segment.subtitle_data or []
        for caption in captions:
            start = max(0.0, float(caption["start"])) + offset
            end = max(start, float(caption["end"]) + offset)
            if end <= start:
                continue
            blocks.append(f"{sequence}\n{_timestamp(start)} --> {_timestamp(end)}\n{caption['text']}\n")
            sequence += 1
        offset += float(segment.duration_seconds)
    return "\n".join(blocks)


def _assemble_wav(segment_paths, output_path):
    if not segment_paths:
        raise RuntimeError("Le projet ne contient aucun segment audio prêt.")
    with wave.open(str(segment_paths[0]), "rb") as first:
        params = first.getparams()
    if params.nchannels != 1 or params.sampwidth != 2 or params.framerate != SAMPLE_RATE:
        raise RuntimeError("Un segment n’est pas au format WAV mono 24 kHz attendu.")
    with wave.open(str(output_path), "wb") as output:
        output.setparams(params)
        for path in segment_paths:
            with wave.open(str(path), "rb") as source:
                if source.getparams()[:4] != params[:4]:
                    raise RuntimeError("Les segments n’ont pas tous le même format audio.")
                while True:
                    frames = source.readframes(65536)
                    if not frames:
                        break
                    output.writeframesraw(frames)
        output.writeframes(b"")


def prepare_project(project_id):
    """Prepare only uncached or modified segments, then persist complete WAV and SRT exports."""
    close_old_connections()
    try:
        with _PIPELINE_LOCK:
            project = NarrationProject.objects.get(pk=project_id)
            voice = project.voice
            segments = list(project.segments.order_by("position", "id"))
            if not voice or not voice.consent_confirmed:
                raise RuntimeError("Choisis une voix dont l’autorisation d’utilisation est confirmée.")
            if not segments or any(not item.text.strip() for item in segments):
                raise RuntimeError("Ajoute un texte à chaque segment avant de préparer la narration.")
            sample_digest = _voice_sample_digest(voice)
            project.total_segments = len(segments)
            project.current_segment = 0
            project.status_message = "Préparation de la narration…"
            project.save(update_fields=["total_segments", "current_segment", "status_message", "updated_at"])
            paths = []
            total_xtts_seconds = 0.0
            total_whisperx_seconds = 0.0
            cached_segments = 0
            with tempfile.TemporaryDirectory(prefix=f"petit-studio-project-{project.pk}-") as folder:
                for index, segment in enumerate(segments, start=1):
                    project.refresh_from_db(fields=["voice_id"])
                    if project.voice_id != voice.pk:
                        raise RuntimeError("La voix du projet a changé pendant le traitement. Relance la préparation.")
                    project.current_segment = index
                    project.status_message = f"Segment {index}/{len(segments)} · vérification du cache"
                    project.save(update_fields=["current_segment", "status_message", "updated_at"])
                    expected_key = cache_key_for(segment.text, voice, sample_digest=sample_digest)

                    def update_progress(stage, fraction, index=index):
                        percent = round(((index - 1 + max(0.0, min(1.0, fraction))) / len(segments)) * 95)
                        NarrationProject.objects.filter(pk=project.pk).update(
                            status_message=f"Segment {index}/{len(segments)} · {stage}",
                            progress_percent=percent,
                        )

                    segment_timings = {}
                    _prepare_segment(segment, voice, expected_key, update_progress, segment_timings)
                    total_xtts_seconds += segment_timings.get("xtts_seconds", 0.0)
                    total_whisperx_seconds += segment_timings.get("whisperx_seconds", 0.0)
                    cached_segments += int(segment_timings.get("cached", False))
                    target = Path(folder) / f"segment-{index:04}.wav"
                    shutil.copy2(segment.audio.path, target)
                    paths.append(target)
                project.refresh_from_db()
                output_wav = Path(folder) / "narration-complete.wav"
                _assemble_wav(paths, output_wav)
                project.status_message = "Assemblage du WAV et du fichier SRT…"
                project.progress_percent = 97
                project.save(update_fields=["status_message", "progress_percent", "updated_at"])
                duration = sum(wav_duration(path) for path in paths)
                srt = srt_text(segments)
                if not srt.strip():
                    raise RuntimeError("Le fichier SRT est vide : aucun sous-titre n’a été exporté.")
                with output_wav.open("rb") as stream:
                    project.full_audio.save(f"narration-{project.pk}.wav", File(stream), save=False)
                project.subtitles.save(f"narration-{project.pk}.srt", ContentFile(srt.encode("utf-8")), save=False)
                project.duration_seconds = duration
                generating_video = project.job_type == NarrationProject.JobType.VIDEO
                project.status = NarrationProject.Status.PROCESSING if generating_video else NarrationProject.Status.READY
                project.queued_at = None
                timings_message = (
                    f"XTTS {total_xtts_seconds:.1f} s · WhisperX {total_whisperx_seconds:.1f} s · "
                    f"cache {cached_segments}/{len(segments)} segment(s)."
                )
                project.status_message = (
                    f"Narration prête · rendu vidéo en cours · {timings_message}"
                    if generating_video else f"Narration prête · {timings_message}"
                )
                project.error_message = ""
                project.progress_percent = 95 if generating_video else 100
                project.current_segment = len(segments)
                project.total_segments = len(segments)
                project.finished_at = None if generating_video else timezone.now()
                project.save(update_fields=[
                    "full_audio", "subtitles", "duration_seconds", "status", "status_message",
                    "error_message", "progress_percent", "current_segment", "total_segments",
                    "queued_at", "finished_at", "updated_at",
                ])
    except Exception as exc:
        NarrationProject.objects.filter(pk=project_id).update(
            status=NarrationProject.Status.ERROR,
            status_message="La préparation de la narration a échoué.",
            error_message=str(exc)[:2000],
            queued_at=None,
            finished_at=timezone.now(),
        )
    finally:
        close_old_connections()
