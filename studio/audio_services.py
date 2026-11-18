"""Safe, synchronous FFmpeg operations for the standalone audio editor."""
import functools
import json
import logging
import math
import shutil
import subprocess
import time
from datetime import timedelta
from pathlib import Path

from django.db import transaction
from django.utils import timezone

from .models import AudioSession
from .services import _ffmpeg_path

logger = logging.getLogger(__name__)

FFMPEG_TIMEOUT_SECONDS = 60
FFPROBE_TIMEOUT_SECONDS = 60
FILTER_PROBE_TIMEOUT_SECONDS = 5
MAX_REVERSE_SECONDS = 5 * 60
MAX_OPERATIONS = 50
OUTPUT_BITRATE = "192k"
MAX_PAD_SEQUENCE_EVENTS = 512

# Tempo and pitch are effect families with a direction parameter. This keeps
# the catalog at 25 effects while offering both faster/slower and higher/lower.
EFFECT_LABELS = {
    "reverse": "Reverse",
    "tempo": "Tempo +/-",
    "pitch": "Pitch +/-",
    "echo_light": "Echo léger",
    "echo_long": "Echo long",
    "reverb_room": "Réverb salle",
    "reverb_cathedral": "Réverb cathédrale",
    "flanger": "Flanger",
    "phaser": "Phaser",
    "tremolo": "Trémolo",
    "vibrato": "Vibrato",
    "lowpass": "Passe-bas",
    "highpass": "Passe-haut",
    "notch": "Notch",
    "bass_boost": "Bass boost",
    "treble_boost": "Treble boost",
    "compressor": "Compresseur",
    "limiter": "Limiteur",
    "loudnorm": "Normalisation",
    "saturation": "Saturation douce",
    "crunch": "Crunch",
    "lofi": "Lo-fi",
    "telephone": "Téléphone",
    "radio": "Radio",
    "eight_bit": "8-bit",
}

EFFECT_FILTERS = {
    "reverse": ("areverse",),
    "tempo": ("atempo",),
    "pitch": ("asetrate", "atempo"),
    "echo_light": ("aecho",),
    "echo_long": ("aecho",),
    "reverb_room": ("aecho",),
    "reverb_cathedral": ("aecho",),
    "flanger": ("flanger",),
    "phaser": ("aphaser",),
    "tremolo": ("tremolo",),
    "vibrato": ("vibrato",),
    "lowpass": ("lowpass",),
    "highpass": ("highpass",),
    "notch": ("bandreject",),
    "bass_boost": ("bass",),
    "treble_boost": ("treble",),
    "compressor": ("acompressor",),
    "limiter": ("alimiter",),
    "loudnorm": ("loudnorm",),
    "saturation": ("acrusher",),
    "crunch": ("acrusher",),
    "lofi": ("acrusher", "lowpass"),
    "telephone": ("highpass", "lowpass"),
    "radio": ("highpass", "lowpass", "bass"),
    "eight_bit": ("acrusher",),
}

PRESET_LABELS = {
    "lofi": "Lo-fi",
    "dreamy": "Rêveur",
    "energetic": "Énergique",
    "cinema": "Cinéma",
    "telephone": "Téléphone",
    "radio": "Radio",
}

PRESET_FILTERS = {
    "lofi": ("acrusher", "lowpass", "atempo"),
    "dreamy": ("aecho", "tremolo"),
    "energetic": ("bass", "treble", "atempo"),
    "cinema": ("aecho", "bass", "acompressor"),
    "telephone": ("highpass", "lowpass"),
    "radio": ("highpass", "lowpass", "bass"),
}


def _ffprobe_path():
    ffmpeg = Path(_ffmpeg_path())
    sibling = ffmpeg.with_name("ffprobe" + ffmpeg.suffix)
    if sibling.is_file():
        return str(sibling)
    path = shutil.which("ffprobe")
    if path:
        return path
    raise RuntimeError("FFprobe est introuvable. Installe-le puis relance l’application.")


@functools.lru_cache(maxsize=4)
def get_available_effects(ffmpeg_path):
    """Return confirmed FFmpeg filters used by the effect and preset catalogs."""
    candidates = set().union(*EFFECT_FILTERS.values(), *PRESET_FILTERS.values())
    available = set()
    for name in sorted(candidates):
        try:
            result = subprocess.run(
                [ffmpeg_path, "-hide_banner", "-h", f"filter={name}"],
                capture_output=True,
                timeout=FILTER_PROBE_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired:
            logger.warning("Timeout détection filtre %s", name)
            continue
        except FileNotFoundError as exc:
            logger.error("FFmpeg est introuvable pendant la détection des filtres : %s", exc)
            return set()

        output = result.stdout or b""
        error = result.stderr or b""
        if isinstance(output, bytes):
            output = output.decode("utf-8", errors="replace")
        if isinstance(error, bytes):
            error = error.decode("utf-8", errors="replace")
        if result.returncode == 0 and "unknown filter" not in f"{output}\n{error}".lower():
            available.add(name)
    return available


def _detect_filters():
    candidates = set().union(*EFFECT_FILTERS.values(), *PRESET_FILTERS.values())
    try:
        ffmpeg_path = _ffmpeg_path()
    except (OSError, RuntimeError) as exc:
        logger.error("FFmpeg est introuvable pendant la détection des filtres : %s", exc)
        available = set()
    else:
        available = get_available_effects(ffmpeg_path)
    unavailable = sorted(candidates - available)
    logger.info("Filtres FFmpeg détectés : %s/%s disponibles", len(available), len(candidates))
    if unavailable:
        logger.warning("Filtres FFmpeg indisponibles : %s", ", ".join(unavailable))
    return frozenset(available)


# Probe filters once when this module starts; the cached result backs UI choices.
AVAILABLE_FILTERS = _detect_filters()


def _filters_available(requirements):
    return set(requirements).issubset(AVAILABLE_FILTERS)


def available_effects():
    return [(key, label) for key, label in EFFECT_LABELS.items() if _filters_available(EFFECT_FILTERS[key])]


def available_presets():
    return [(key, label) for key, label in PRESET_LABELS.items() if _filters_available(PRESET_FILTERS[key])]


def probe_audio(path, *, timeout=FFPROBE_TIMEOUT_SECONDS):
    """Return source audio duration and sample rate, rejecting unreadable files."""
    try:
        result = subprocess.run(
            [
                _ffprobe_path(), "-v", "error", "-select_streams", "a:0",
                "-show_entries", "stream=sample_rate,duration:format=duration", "-of", "json", str(path),
            ],
            capture_output=True, text=True, check=False, timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise TimeoutError("L’analyse audio a dépassé le délai maximal de 60 secondes.") from exc
    if result.returncode:
        detail = (result.stderr or "").strip().splitlines()[-1:]
        raise ValueError("FFprobe ne reconnaît pas ce fichier audio." + (f" ({detail[0]})" if detail else ""))
    try:
        metadata = json.loads(result.stdout)
        streams = metadata.get("streams") or []
        if not streams:
            raise ValueError
        duration = float(streams[0].get("duration") or metadata.get("format", {}).get("duration"))
        sample_rate = int(streams[0].get("sample_rate") or 48000)
        if not math.isfinite(duration) or duration <= 0 or sample_rate <= 0:
            raise ValueError
        return duration, sample_rate
    except (TypeError, ValueError, KeyError, json.JSONDecodeError) as exc:
        raise ValueError("La durée ou la fréquence d’échantillonnage de l’audio est illisible.") from exc


def _number(value, name, minimum, maximum):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"Paramètre audio invalide : {name}.")
    value = float(value)
    if not math.isfinite(value) or not minimum <= value <= maximum:
        raise ValueError(f"Paramètre audio hors limites : {name}.")
    return value


def _effect_filters(name, params, sample_rate):
    if name not in EFFECT_LABELS or not _filters_available(EFFECT_FILTERS[name]):
        raise ValueError("Cet effet audio n’est pas disponible avec le FFmpeg installé.")
    if name in {"tempo", "pitch"}:
        if (set(params) != {"direction"} or not isinstance(params["direction"], str)
                or params["direction"] not in {"up", "down"}):
            raise ValueError("Direction d’effet audio invalide.")
        direction = params["direction"]
        if name == "tempo":
            factor = 1.2 if direction == "up" else 1 / 1.2
            return [f"atempo={factor:.8f}"]
        semitones = 2 if direction == "up" else -2
        factor = 2 ** (semitones / 12)
        changed_rate = round(sample_rate * factor)
        return [f"asetrate={changed_rate}", f"atempo={1 / factor:.8f}"]
    if params:
        raise ValueError("Cet effet audio ne prend pas de paramètres personnalisés.")
    return {
        "reverse": ["areverse"],
        "echo_light": ["aecho=0.8:0.9:800:0.2"],
        "echo_long": ["aecho=0.8:0.9:1500:0.4"],
        "reverb_room": ["aecho=0.6:0.8:300:0.3"],
        "reverb_cathedral": ["aecho=0.5:0.9:800:0.6"],
        # FFmpeg names these filters `flanger` and `tremolo` (without an `a` prefix).
        "flanger": ["flanger"],
        "phaser": ["aphaser"],
        "tremolo": ["tremolo=f=5:d=0.7"],
        "vibrato": ["vibrato=f=5:d=0.5"],
        "lowpass": ["lowpass=f=1000"],
        "highpass": ["highpass=f=1000"],
        "notch": ["bandreject=f=1000:w=200"],
        "bass_boost": ["bass=g=5"],
        "treble_boost": ["treble=g=5"],
        "compressor": ["acompressor"],
        "limiter": ["alimiter"],
        "loudnorm": ["loudnorm"],
        "saturation": ["acrusher=bits=8:mix=0.2"],
        "crunch": ["acrusher=bits=6:mix=0.5"],
        "lofi": ["acrusher=bits=4:mix=0.7", "lowpass=f=4000"],
        "telephone": ["highpass=f=300", "lowpass=f=3400"],
        "radio": ["highpass=f=200", "lowpass=f=5000", "bass=g=3"],
        "eight_bit": ["acrusher=bits=3:mix=0.8"],
    }[name]


def _preset_filters(name, sample_rate):
    if name not in PRESET_LABELS or not _filters_available(PRESET_FILTERS[name]):
        raise ValueError("Ce preset audio n’est pas disponible avec le FFmpeg installé.")
    return {
        "lofi": ["acrusher=bits=4:mix=0.7", "lowpass=f=4000", "atempo=0.95"],
        "dreamy": ["aecho=0.7:0.8:1000:0.4", "tremolo=f=0.5:d=0.3"],
        "energetic": ["bass=g=6", "treble=g=4", "atempo=1.1"],
        "cinema": ["aecho=0.5:0.9:800:0.6", "bass=g=3", "acompressor"],
        "telephone": ["highpass=f=300", "lowpass=f=3400"],
        "radio": ["highpass=f=200", "lowpass=f=5000", "bass=g=3"],
    }[name]


def build_filter_chain(operations, source_duration, sample_rate):
    """Validate operations and produce the canonical trim/fade/volume/effects chain."""
    if not isinstance(operations, list) or len(operations) > MAX_OPERATIONS:
        raise ValueError("La liste des opérations audio est invalide.")
    if any(not isinstance(operation, dict) or set(operation) != {"type", "params"}
           or not isinstance(operation["type"], str)
           or not isinstance(operation["params"], dict) for operation in operations):
        raise ValueError("Une opération audio est mal formée.")

    category_order = {"trim": 0, "fade": 1, "volume": 2, "effect": 3, "preset": 3}
    if any(operation["type"] not in category_order for operation in operations):
        raise ValueError("Type d’opération audio inconnu.")
    ordered = sorted(enumerate(operations), key=lambda pair: (category_order[pair[1]["type"]], pair[0]))
    filters = []
    start, end = 0.0, source_duration
    trim_seen = False
    for _index, operation in ordered:
        if operation["type"] != "trim":
            continue
        if trim_seen or set(operation["params"]) != {"start", "end"}:
            raise ValueError("Une seule opération de découpe avec début et fin est autorisée.")
        trim_seen = True
        start = _number(operation["params"]["start"], "début de découpe", 0, source_duration)
        end = _number(operation["params"]["end"], "fin de découpe", 0, source_duration)
        if start >= end:
            raise ValueError("La fin de découpe doit être après le début.")
    if trim_seen:
        filters.extend([f"atrim=start={start:.6f}:end={end:.6f}", "asetpts=PTS-STARTPTS"])
    working_duration = end - start

    fade_seen = volume_seen = False
    for _index, operation in ordered:
        kind, params = operation["type"], operation["params"]
        if kind == "fade":
            if fade_seen or set(params) != {"in", "out"}:
                raise ValueError("Une seule opération de fondu avec entrée et sortie est autorisée.")
            fade_seen = True
            fade_in = _number(params["in"], "fondu d’entrée", 0, working_duration)
            fade_out = _number(params["out"], "fondu de sortie", 0, working_duration)
            if fade_in:
                filters.append(f"afade=t=in:st=0:d={fade_in:.6f}")
            if fade_out:
                filters.append(f"afade=t=out:st={max(0, working_duration - fade_out):.6f}:d={fade_out:.6f}")
        elif kind == "volume":
            if volume_seen or set(params) != {"percent"}:
                raise ValueError("Une seule opération de volume est autorisée.")
            volume_seen = True
            percent = _number(params["percent"], "volume", 0, 200)
            filters.append(f"volume={percent / 100:.6f}")
        elif kind == "effect":
            if set(params) not in ({"name"}, {"name", "direction"}):
                raise ValueError("Paramètres d’effet audio invalides.")
            name = params["name"]
            if not isinstance(name, str):
                raise ValueError("Nom d’effet audio invalide.")
            effect_params = {key: value for key, value in params.items() if key != "name"}
            if name == "reverse" and working_duration > MAX_REVERSE_SECONDS:
                raise ValueError("Fichier trop long pour l’effet reverse (max 5 min).")
            filters.extend(_effect_filters(name, effect_params, sample_rate))
        elif kind == "preset":
            if set(params) != {"name"}:
                raise ValueError("Paramètres de preset audio invalides.")
            name = params["name"]
            if not isinstance(name, str):
                raise ValueError("Nom de preset audio invalide.")
            filters.extend(_preset_filters(name, sample_rate))
    return ",".join(filters), working_duration


def apply_operations(input_path, operations, output_path):
    """Apply a validated operation list synchronously and return (path, duration)."""
    input_path, output_path = Path(input_path), Path(output_path)
    deadline = time.monotonic() + FFMPEG_TIMEOUT_SECONDS

    def remaining_timeout():
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Le traitement audio a dépassé le délai maximal de 60 secondes.")
        return remaining

    source_duration, sample_rate = probe_audio(input_path, timeout=remaining_timeout())
    filter_chain, _base_duration = build_filter_chain(operations, source_duration, sample_rate)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    command = [
        _ffmpeg_path(), "-y", "-v", "error", "-i", str(input_path), "-map", "0:a:0", "-vn",
    ]
    if filter_chain:
        command.extend(["-af", filter_chain])
    command.extend(["-c:a", "aac", "-b:a", OUTPUT_BITRATE, str(output_path)])
    try:
        result = subprocess.run(
            command, capture_output=True, text=True, check=False, timeout=remaining_timeout(),
        )
    except subprocess.TimeoutExpired as exc:
        output_path.unlink(missing_ok=True)
        raise TimeoutError("Le traitement audio a dépassé le délai maximal de 60 secondes.") from exc
    if result.returncode or not output_path.is_file() or output_path.stat().st_size == 0:
        output_path.unlink(missing_ok=True)
        detail = (result.stderr or "").strip().splitlines()[-1:]
        raise RuntimeError("Traitement audio échoué." + (f" {detail[0]}" if detail else ""))
    try:
        output_duration, _output_sample_rate = probe_audio(
            output_path, timeout=remaining_timeout(),
        )
    except Exception:
        output_path.unlink(missing_ok=True)
        raise
    return output_path, output_duration


def render_pad_sequence(source_audio_path, sequence_data, pad_boundaries, output_path):
    """Render a recorded pad performance as an AAC/M4A track.

    ``sequence_data`` contains ``recording_duration_ms``, ``bpm`` and ``events``.
    Each event selects one of the supplied (start, end) source boundaries and
    starts it at ``timestamp_ms``. BPM changes playback speed and pitch together,
    matching the browser's vinyl-style sampler. The final duration preserves
    both the recorded tail silence and any pad that is still playing at Stop.
    """
    source_audio_path, output_path = Path(source_audio_path), Path(output_path)
    if not isinstance(sequence_data, dict) or set(sequence_data) != {
        "version", "recording_duration_ms", "bpm", "events",
    }:
        raise ValueError("Données de séquence invalides.")
    if sequence_data["version"] != 1:
        raise ValueError("Version de séquence non prise en charge.")
    bpm = sequence_data["bpm"]
    if isinstance(bpm, bool) or not isinstance(bpm, int) or not 60 <= bpm <= 200:
        raise ValueError("Le BPM doit être un entier compris entre 60 et 200.")
    playback_rate = bpm / 120
    recording_duration_ms = sequence_data["recording_duration_ms"]
    if (isinstance(recording_duration_ms, bool)
            or not isinstance(recording_duration_ms, int)
            or recording_duration_ms < 0):
        raise ValueError("Durée d’enregistrement invalide.")
    events = sequence_data["events"]
    if not isinstance(events, list) or not events:
        raise ValueError("La séquence ne contient aucun pad joué.")
    if len(events) > MAX_PAD_SEQUENCE_EVENTS:
        raise ValueError(f"La séquence dépasse la limite de {MAX_PAD_SEQUENCE_EVENTS} déclenchements.")
    if not isinstance(pad_boundaries, (list, tuple)) or len(pad_boundaries) != 16:
        raise ValueError("Les 16 bornes des pads sont requises.")

    parsed_events = []
    for event in events:
        if not isinstance(event, dict) or set(event) != {"pad_index", "timestamp_ms"}:
            raise ValueError("Événement de pad invalide.")
        pad_index, timestamp_ms = event["pad_index"], event["timestamp_ms"]
        if isinstance(pad_index, bool) or not isinstance(pad_index, int) or not 0 <= pad_index < 16:
            raise ValueError("Indice de pad invalide.")
        if isinstance(timestamp_ms, bool) or not isinstance(timestamp_ms, int) or timestamp_ms < 0:
            raise ValueError("Horodatage de pad invalide.")
        if timestamp_ms > recording_duration_ms:
            raise ValueError("Un pad ne peut pas être déclenché après l’arrêt de l’enregistrement.")
        parsed_events.append((pad_index, timestamp_ms))

    deadline = time.monotonic() + FFMPEG_TIMEOUT_SECONDS

    def remaining_timeout():
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Le rendu de la séquence a dépassé le délai maximal de 60 secondes.")
        return remaining

    source_duration, sample_rate = probe_audio(source_audio_path, timeout=remaining_timeout())
    adjusted_sample_rate = round(sample_rate * playback_rate)
    normalized_boundaries = []
    for boundary in pad_boundaries:
        if not isinstance(boundary, (list, tuple)) or len(boundary) != 2:
            raise ValueError("Borne de pad invalide.")
        start = _number(boundary[0], "pad_start", 0, source_duration)
        end = _number(boundary[1], "pad_end", 0, source_duration)
        if end <= start:
            raise ValueError("La fin d’un pad doit être après son début.")
        normalized_boundaries.append((start, end))

    validated_events = []
    output_duration = recording_duration_ms / 1000
    for pad_index, timestamp_ms in parsed_events:
        start, end = normalized_boundaries[pad_index]
        timestamp = timestamp_ms / 1000
        output_duration = max(output_duration, timestamp + (end - start) / playback_rate)
        validated_events.append((start, end, timestamp_ms))

    if output_duration <= 0:
        raise ValueError("La durée de la séquence doit être positive.")

    labels = [f"pad{index}" for index in range(len(validated_events))]
    graph = [f"[0:a]asplit={len(labels)}" + "".join(f"[{label}]" for label in labels)]
    for index, ((start, end, timestamp_ms), label) in enumerate(zip(validated_events, labels)):
        graph.append(
            f"[{label}]atrim=start={start:.6f}:end={end:.6f},"
            f"asetrate={adjusted_sample_rate},aresample={sample_rate},"
            f"asetpts=PTS-STARTPTS,adelay={timestamp_ms}:all=1[event{index}]"
        )
    inputs = "".join(f"[event{index}]" for index in range(len(labels)))
    graph.append(
        f"{inputs}amix=inputs={len(labels)}:duration=longest:normalize=0:dropout_transition=0,"
        f"apad=whole_dur={output_duration:.6f},atrim=duration={output_duration:.6f}[mixed]"
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    command = [
        _ffmpeg_path(), "-y", "-v", "error", "-i", str(source_audio_path),
        "-filter_complex", ";".join(graph), "-map", "[mixed]",
        "-vn", "-t", f"{output_duration:.6f}", "-c:a", "aac",
        "-b:a", OUTPUT_BITRATE, str(output_path),
    ]
    output_path.unlink(missing_ok=True)
    try:
        result = subprocess.run(
            command, capture_output=True, text=True, check=False, timeout=remaining_timeout(),
        )
    except subprocess.TimeoutExpired as exc:
        output_path.unlink(missing_ok=True)
        raise TimeoutError("Le rendu de la séquence a dépassé le délai maximal de 60 secondes.") from exc
    except OSError as exc:
        output_path.unlink(missing_ok=True)
        raise RuntimeError(f"Impossible de lancer FFmpeg pour la séquence : {exc}") from exc
    if result.returncode or not output_path.is_file() or output_path.stat().st_size == 0:
        output_path.unlink(missing_ok=True)
        detail = (result.stderr or "").strip().splitlines()[-1:]
        raise RuntimeError("Rendu de la séquence échoué." + (f" {detail[0]}" if detail else ""))
    try:
        actual_duration, _sample_rate = probe_audio(output_path, timeout=remaining_timeout())
    except Exception:
        output_path.unlink(missing_ok=True)
        raise
    return output_path, actual_duration


def _delete_audio_session_files(session):
    """Remove a session's source and all its versioned previews after commit."""
    files = []
    for field_name in ("source_file", "current_preview_file"):
        field = getattr(session, field_name)
        if field and field.name:
            files.append((field.storage, field.name))

    # Preview names include this stable session prefix so replaced previews are
    # retained during editing but can all be removed when the session expires.
    preview_directory = f"audio-sessions/user_{session.user_id}"
    preview_prefix = f"session_{session.pk}_"
    preview_storage = session.current_preview_file.storage

    def delete_files():
        try:
            _directories, names = preview_storage.listdir(preview_directory)
            files.extend(
                (preview_storage, f"{preview_directory}/{name}")
                for name in names if name.startswith(preview_prefix)
            )
        except Exception:
            logger.warning(
                "Impossible de lister les aperçus de la session audio %s.",
                session.pk,
                exc_info=True,
            )
        for storage, name in files:
            try:
                storage.delete(name)
            except Exception:
                logger.warning("Impossible de supprimer le fichier de session audio %s.", name, exc_info=True)

    if files:
        transaction.on_commit(delete_files)


def cleanup_expired_audio_sessions(user=None, *, now=None, max_idle=timedelta(hours=24)):
    """Delete sessions inactive for 24 hours, cleaning files only after commit."""
    now = now or timezone.now()
    cutoff = now - max_idle
    expired = AudioSession.objects.filter(updated_at__lt=cutoff)
    if user is not None:
        expired = expired.filter(user=user)
    deleted = 0
    for session_id in expired.values_list("pk", flat=True).iterator():
        with transaction.atomic():
            session = AudioSession.objects.select_for_update().filter(
                pk=session_id, updated_at__lt=cutoff,
            )
            if user is not None:
                session = session.filter(user=user)
            session = session.first()
            if session is None:
                continue
            _delete_audio_session_files(session)
            session.delete()
            deleted += 1
    return deleted
