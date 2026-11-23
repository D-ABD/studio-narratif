"""Local FFmpeg rendering for narrated scenes and imported video clips."""
import hashlib
import json
import math
import shlex
import shutil
import subprocess
import sys
import threading
import tempfile
import time
from pathlib import Path

from django.conf import settings
from django.core.files import File
from django.db import close_old_connections
from django.utils import timezone

from .models import (
    AudioAsset, NarrationProject, NarrationSegment, default_assembly_animation_settings,
)
from .services import _ffmpeg_path, wav_duration
from .job_control import (
    JobCancelled, cancellation_checker, check_job_cancellation, mark_job_cancelled,
)


FFPROBE_TIMEOUT_SECONDS = 60
FFMPEG_TIMEOUT_SECONDS = 600
VIDEO_ENCODER_CHECK_TIMEOUT_SECONDS = 15

_VIDEO_ENCODER_CACHE = {}
_VIDEO_ENCODER_LOCK = threading.Lock()


class _FFmpegCommandError(RuntimeError):
    """FFmpeg exited unsuccessfully after starting the selected video encoder."""

    def __init__(self, message, *, stderr="", command=None):
        super().__init__(message)
        self.stderr = stderr
        self.command = command


def _video_encoder(ffmpeg):
    """Use VideoToolbox on macOS when this FFmpeg binary advertises the encoder."""
    if sys.platform != "darwin":
        return "libx264"

    with _VIDEO_ENCODER_LOCK:
        if ffmpeg in _VIDEO_ENCODER_CACHE:
            return _VIDEO_ENCODER_CACHE[ffmpeg]

        try:
            result = subprocess.run(
                [ffmpeg, "-hide_banner", "-encoders"],
                capture_output=True,
                text=True,
                check=False,
                timeout=VIDEO_ENCODER_CHECK_TIMEOUT_SECONDS,
            )
            encoder_list = (result.stdout or "") + "\n" + (result.stderr or "")
            available = result.returncode == 0 and any(
                "h264_videotoolbox" in line.split()
                for line in encoder_list.splitlines()
            )
        except (OSError, subprocess.TimeoutExpired):
            available = False

        encoder = "h264_videotoolbox" if available else "libx264"
        _VIDEO_ENCODER_CACHE[ffmpeg] = encoder
        return encoder


def _video_encoder_args(ffmpeg, width, height):
    """Return codec-specific arguments for the selected encoder."""
    if _video_encoder(ffmpeg) == "h264_videotoolbox":
        # 1080p outputs use the higher target bitrate; smaller outputs stay at 10 Mbps.
        bitrate = "10M" if width * height <= 1280 * 720 else "15M"
        return ["-c:v", "h264_videotoolbox", "-b:v", bitrate]
    return ["-c:v", "libx264", "-preset", "ultrafast", "-crf", "24"]


def _software_fallback_command(command):
    """Replace VideoToolbox options with the existing libx264 speed/quality settings."""
    try:
        codec_index = command.index("-c:v")
    except ValueError:
        return None
    if codec_index + 1 >= len(command) or command[codec_index + 1] != "h264_videotoolbox":
        return None

    fallback = list(command)
    bitrate_index = next(
        (index for index, argument in enumerate(fallback) if argument == "-b:v"),
        None,
    )
    if bitrate_index is not None:
        del fallback[bitrate_index:bitrate_index + 2]
        codec_index = fallback.index("-c:v")
    fallback[codec_index + 1:codec_index + 2] = [
        "libx264", "-preset", "ultrafast", "-crf", "24",
    ]
    return fallback


def _remove_partial_output(command, cwd):
    """Remove a possibly incomplete output before retrying with libx264."""
    if not command:
        return
    output_path = Path(command[-1])
    if not output_path.is_absolute():
        output_path = Path(cwd) / output_path
    try:
        output_path.unlink(missing_ok=True)
    except OSError:
        pass


def _dimensions(video_format):
    return (1080, 1920) if video_format == NarrationProject.VideoFormat.VERTICAL else (1280, 720)


def _light_breathing_filter():
    """Add a very subtle brightness pulse once per scene."""
    return "eq=brightness='0.018*sin(2*PI*t/8)':eval=frame"


def _color_grading_filters():
    """Apply a restrained contrast/saturation lift and a subtle edge vignette."""
    return ["eq=contrast=1.04:saturation=1.05", "vignette=angle=PI/16"]


def _movement_filters(motion, width, height, duration, camera_profile="linear", light_breathing=False):
    """Build camera movement and optional light breathing filters for an image scene."""
    organic = camera_profile == NarrationSegment.CameraProfile.ORGANIC
    if motion == "still":
        filters = [
            f"scale={width}:{height}:force_original_aspect_ratio=decrease",
            f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black",
            "fps=24",
        ]
    else:
        frames = max(1, round(max(0.1, duration) * 24))
        linear_progress = f"min(on/{frames},1)"
        progress = f"(0.5-0.5*cos(PI*{linear_progress}))" if organic else linear_progress
        base = [f"scale={width}:{height}:force_original_aspect_ratio=increase", f"crop={width}:{height}"]

        if motion == "zoom_in":
            zoom = f"1.02+0.06*{progress}" if organic else "min(zoom+0.00010,1.08)"
            filters = base + [f"zoompan=z='{zoom}':d=1:s={width}x{height}:fps=24"]
        elif motion == "zoom_out":
            zoom = f"1.08-0.06*{progress}" if organic else "if(eq(on,1),1.08,max(zoom-0.00010,1.0))"
            filters = base + [f"zoompan=z='{zoom}':d=1:s={width}x{height}:fps=24"]
        elif motion == "pan_h":
            filters = base + [
                f"zoompan=z='1.08':x='(iw-iw/zoom)*{progress}':y='(ih-ih/zoom)/2':d=1:s={width}x{height}:fps=24"
            ]
        elif motion == "pan_v":
            filters = base + [
                f"zoompan=z='1.08':x='(iw-iw/zoom)/2':y='(ih-ih/zoom)*{progress}':d=1:s={width}x{height}:fps=24"
            ]
        elif motion == "zoom_pan":
            zoom = f"1.02+0.06*{progress}" if organic else f"1+0.08*{progress}"
            filters = base + [
                f"zoompan=z='{zoom}':x='(iw-iw/zoom)*{progress}':y='(ih-ih/zoom)*{progress}':d=1:s={width}x{height}:fps=24"
            ]
        else:
            raise RuntimeError("Le mouvement choisi pour une scène n’est pas reconnu.")

        if organic:
            filters.extend([
                "rotate=angle='0.0035*sin(2*PI*t/8)':ow=rotw(iw):oh=roth(ih):c=black",
                f"crop={width}:{height}",
            ])

    if light_breathing:
        filters.append(_light_breathing_filter())
    return filters


def _assembly_movement_filters(motion, width, height, duration):
    """Return a Ken Burns filter chain for an assembly still, or None for no effect."""
    if motion in {NarrationSegment.AssemblyMotion.ZOOM_IN, NarrationSegment.AssemblyMotion.ZOOM_OUT}:
        return _movement_filters(motion, width, height, duration)
    if motion in {NarrationSegment.AssemblyMotion.PAN_LEFT, NarrationSegment.AssemblyMotion.PAN_RIGHT}:
        frames = max(1, round(max(0.1, duration) * 24))
        progress = f"min(on/{frames},1)"
        # x moves the crop window: decreasing x pans the camera left, increasing x right.
        x_progress = f"(1-{progress})" if motion == NarrationSegment.AssemblyMotion.PAN_LEFT else progress
        return [
            f"scale={width}:{height}:force_original_aspect_ratio=increase",
            f"crop={width}:{height}",
            f"zoompan=z='1.08':x='(iw-iw/zoom)*{x_progress}':y='(ih-ih/zoom)/2':d=1:s={width}x{height}:fps=24",
        ]
    # Parallax is intentionally listed in the UI but remains a no-op in this release.
    return None


def _assembly_animation_filters(animation_settings, width, height, duration):
    effects = (animation_settings or {}).get("effects", {})
    active_effects = (
        config for name, config in effects.items()
        if name != "parallax" and isinstance(config, dict)
    )
    if not any(config.get("enabled") for config in active_effects):
        return None
    return _animation_filter_chain(
        animation_settings, width, height, duration, apply_color_grading=False,
    )


def _assembly_animation_settings_for_segment(segment):
    settings = segment.assembly_animation_settings or default_assembly_animation_settings()
    effects = settings.get("effects", {})
    if any(config.get("enabled") for config in effects.values() if isinstance(config, dict)):
        return settings

    # Keep compatibility with integrations and older rows that only set assembly_motion.
    motion = segment.assembly_motion
    if motion == NarrationSegment.AssemblyMotion.NONE:
        return settings
    effects = settings["effects"]
    if motion == NarrationSegment.AssemblyMotion.ZOOM_IN:
        effects["zoom"].update(enabled=True, direction="in")
    elif motion == NarrationSegment.AssemblyMotion.ZOOM_OUT:
        effects["zoom"].update(enabled=True, direction="out")
    elif motion == NarrationSegment.AssemblyMotion.PAN_LEFT:
        effects["pan"].update(enabled=True, direction="horizontal_reverse")
    elif motion == NarrationSegment.AssemblyMotion.PAN_RIGHT:
        effects["pan"].update(enabled=True, direction="horizontal")
    elif motion == NarrationSegment.AssemblyMotion.PARALLAX:
        effects["parallax"]["enabled"] = True
    return settings




ANIMATION_LEVELS = {
    "slow": {"zoom": 0.035, "pan": 0.035, "handheld": 0.0015, "light": 0.025, "period": 8},
    "medium": {"zoom": 0.065, "pan": 0.065, "handheld": 0.0030, "light": 0.045, "period": 6},
    "fast": {"zoom": 0.10, "pan": 0.10, "handheld": 0.0050, "light": 0.065, "period": 4},
}


def _camera_effect_filters(settings, width, height, duration):
    """Build a shared camera chain for image and parallax base frames."""
    effects = (settings or {}).get("effects", {})
    profile = (settings or {}).get("camera_profile", "linear")
    zoom = effects.get("zoom", {})
    pan = effects.get("pan", {})
    handheld = effects.get("handheld", {})
    active_camera = zoom.get("enabled") or pan.get("enabled") or handheld.get("enabled")
    filters = []
    if active_camera:
        filters.extend([
            f"scale={round(width * 1.12)}:{round(height * 1.12)}:force_original_aspect_ratio=increase",
            f"crop={round(width * 1.12)}:{round(height * 1.12)}",
        ])
        frames = max(1, round(max(0.1, duration) * 24))
        progress = f"min(on/{frames},1)"
        if profile == "organic":
            progress = f"(0.5-0.5*cos(PI*{progress}))"
        zoom_on = zoom.get("enabled", False)
        pan_on = pan.get("enabled", False)
        zoom_level = ANIMATION_LEVELS.get(zoom.get("level"), ANIMATION_LEVELS["medium"])["zoom"]
        pan_level = ANIMATION_LEVELS.get(pan.get("level"), ANIMATION_LEVELS["medium"])["pan"]
        baseline = 1.12 / (1.0 + (zoom_level if zoom_on else pan_level if pan_on else 0.0))
        if zoom_on and zoom.get("direction") == "out":
            zoom_expr = f"{baseline + zoom_level:.5f}-{zoom_level:.5f}*{progress}"
        elif zoom_on:
            zoom_expr = f"{baseline:.5f}+{zoom_level:.5f}*{progress}"
        elif pan_on:
            zoom_expr = f"{baseline + pan_level:.5f}"
        else:
            zoom_expr = "1.04"
        x_expr = "iw/2-(iw/zoom/2)"
        y_expr = "ih/2-(ih/zoom/2)"
        if pan_on:
            direction = pan.get("direction")
            if direction in {"horizontal", "diagonal", "diagonal_reverse"}:
                x_progress = f"(1-{progress})" if direction in {"horizontal_reverse", "diagonal_reverse"} else progress
                x_expr = f"(iw-iw/zoom)*{x_progress}"
            elif direction == "horizontal_reverse":
                x_expr = f"(iw-iw/zoom)*(1-{progress})"
            if direction in {"vertical", "diagonal", "diagonal_reverse"}:
                y_progress = f"(1-{progress})" if direction == "diagonal_reverse" else progress
                y_expr = f"(ih-ih/zoom)*{y_progress}"
            elif direction == "vertical_reverse":
                y_expr = f"(ih-ih/zoom)*(1-{progress})"
        filters.append(
            f"zoompan=z='{zoom_expr}':x='{x_expr}':y='{y_expr}':d=1:s={width}x{height}:fps=24"
        )
    else:
        filters.extend([
            f"scale={width}:{height}:force_original_aspect_ratio=decrease",
            f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black",
            "fps=24",
        ])
    if handheld.get("enabled"):
        level = ANIMATION_LEVELS.get(handheld.get("level"), ANIMATION_LEVELS["medium"])
        # Rotate inside an enlarged canvas and crop back to the output dimensions.
        filters.extend([
            f"scale={round(width * 1.08)}:{round(height * 1.08)}",
            f"rotate=angle='{level['handheld']:.5f}*sin(2*PI*t/2.7)+{level['handheld'] * 0.45:.5f}*sin(2*PI*t/1.3)':ow=rotw(iw):oh=roth(ih):c=black",
            f"crop={width}:{height}",
        ])
    return filters


def _light_effect_filters(settings):
    effects = (settings or {}).get("effects", {})
    filters = []
    breathing = effects.get("light_breathing", {})
    if breathing.get("enabled"):
        level = ANIMATION_LEVELS.get(breathing.get("level"), ANIMATION_LEVELS["medium"])
        filters.append(
            f"eq=brightness='{level['light']:.4f}*sin(2*PI*t/{level['period']})'"
        )
    leak = effects.get("light_leak", {})
    if leak.get("enabled"):
        level = ANIMATION_LEVELS.get(leak.get("level"), ANIMATION_LEVELS["medium"])
        period = level["period"] * 1.7
        # A warm, feathered band drifts across the frame without external overlay assets.
        mask = (
            f"exp(-pow((X-W*(0.18+0.64*(0.5+0.5*sin(2*PI*T/{period:.1f}))))/(W*0.22),2)"
            f"-pow((Y-H*0.38)/(H*0.72),2))"
        )
        strength = 16 + round(level["light"] * 180)
        filters.append(
            "format=rgb24,"
            f"geq=r='clip(r(X,Y)+{strength}*{mask},0,255)':"
            f"g='clip(g(X,Y)+{round(strength * 0.58)}*{mask},0,255)':"
            f"b='clip(b(X,Y)+{round(strength * 0.20)}*{mask},0,255)'"
        )
    return filters


def _animation_filter_chain(
    settings, width, height, duration, subtitle_file=None, *, apply_color_grading=True,
):
    filters = _camera_effect_filters(settings, width, height, duration)
    filters.extend(_light_effect_filters(settings))
    if apply_color_grading:
        filters.extend(_color_grading_filters())
    if subtitle_file:
        filters.append(_subtitle_filter(subtitle_file))
    return filters


def _terminate_subprocess(process, grace_seconds=3):
    if process.poll() is not None:
        return
    try:
        process.terminate()
    except OSError:
        pass
    try:
        process.communicate(timeout=grace_seconds)
    except subprocess.TimeoutExpired:
        try:
            process.kill()
        except OSError:
            pass
        process.communicate()


def _command_video_encoder(command):
    try:
        return command[command.index("-c:v") + 1]
    except (ValueError, IndexError):
        try:
            return command[command.index("-c") + 1]
        except (ValueError, IndexError):
            return "default"


def _run_ffmpeg_once(command, cwd, operation, cancel_check=None):
    if cancel_check and cancel_check():
        raise JobCancelled("Traitement annulé à la demande de l’utilisateur.")
    encoder = _command_video_encoder(command)
    command_line = shlex.join([str(argument) for argument in command])
    print(
        f"[FFmpeg] Début · {operation} · encodeur={encoder}\n[FFmpeg] Commande complète : {command_line}",
        flush=True,
    )
    try:
        process = subprocess.Popen(
            command, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
    except OSError as exc:
        raise RuntimeError(f"Impossible de lancer FFmpeg pour {operation} : {exc}") from exc

    started = time.perf_counter()
    deadline = started + FFMPEG_TIMEOUT_SECONDS
    stderr = ""
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                _terminate_subprocess(process)
                raise RuntimeError(f"FFmpeg a dépassé le délai de 10 minutes pendant {operation}.")
            try:
                _stdout, stderr = process.communicate(timeout=min(0.5, remaining))
                break
            except subprocess.TimeoutExpired:
                if cancel_check and cancel_check():
                    _terminate_subprocess(process)
                    raise JobCancelled("Traitement annulé à la demande de l’utilisateur.")
                if time.monotonic() >= deadline:
                    _terminate_subprocess(process)
                    raise RuntimeError(f"FFmpeg a dépassé le délai de 10 minutes pendant {operation}.")
    finally:
        elapsed = time.perf_counter() - started
        print(
            f"[FFmpeg] Fin · {operation} · encodeur={encoder} · durée={elapsed:.2f}s · "
            f"code_retour={process.poll()}",
            flush=True,
        )

    if process.returncode:
        complete_stderr = (stderr or "").strip()
        detail = complete_stderr.splitlines()[-1] if complete_stderr else f"code de sortie {process.returncode}"
        if "No such filter" in complete_stderr and "subtitles" in complete_stderr:
            raise RuntimeError("FFmpeg ne prend pas en charge l’incrustation des sous-titres (filtre libass). Réinstalle FFmpeg avec le support libass.")
        raise _FFmpegCommandError(
            f"FFmpeg a échoué pendant {operation} : {detail}",
            stderr=complete_stderr,
            command=list(command),
        )


def _run_ffmpeg(command, cwd, operation, cancel_check=None):
    try:
        _run_ffmpeg_once(command, cwd, operation, cancel_check=cancel_check)
        return False
    except _FFmpegCommandError as exc:
        fallback = _software_fallback_command(command)
        if fallback is None:
            raise
        print(
            f"[FFmpeg] Fallback VideoToolbox → libx264 · opération={operation} · "
            f"erreur exacte :\n{exc.stderr or str(exc)}",
            flush=True,
        )
        _remove_partial_output(command, cwd)
        _run_ffmpeg_once(fallback, cwd, operation, cancel_check=cancel_check)
        return True


ASS_PLAY_RES_X = 1080
ASS_PLAY_RES_Y = 1920
ASS_MAX_LINE_CHARS = 30
ASS_WHITE = "&H00FFFFFF&"
ASS_ORANGE = "&H000B9EF5&"


def _ass_time(seconds):
    centiseconds = max(0, round(float(seconds) * 100))
    total_seconds, fraction = divmod(centiseconds, 100)
    hours, remainder = divmod(total_seconds, 3600)
    minutes, whole_seconds = divmod(remainder, 60)
    return f"{hours}:{minutes:02}:{whole_seconds:02}.{fraction:02}"


def _ass_escape_text(value):
    # Keep generated subtitle text from being parsed as ASS override syntax.
    return str(value).replace("\\", "／").replace("{", "｛").replace("}", "｝")


def _ass_word_groups(words):
    """Split aligned words into chunks that fit in no more than two display lines."""
    groups, current, line_length, lines = [], [], 0, 1
    for item in words:
        word = (item.get("word") or "").strip()
        if not word or item.get("start") is None or item.get("end") is None:
            continue
        try:
            start, end = float(item["start"]), float(item["end"])
        except (TypeError, ValueError):
            continue
        if end <= start:
            continue
        item = {"word": word, "start": start, "end": end}
        token_length = len(_ass_escape_text(word))
        next_lines = lines + int(bool(current) and line_length + 1 + token_length > ASS_MAX_LINE_CHARS)
        if current and next_lines > 2:
            groups.append(current)
            current, line_length, lines = [item], token_length, 1
        else:
            current.append(item)
            if len(current) == 1 or line_length + 1 + token_length > ASS_MAX_LINE_CHARS:
                lines += int(len(current) > 1)
                line_length = token_length
            else:
                line_length += 1 + token_length
    if current:
        groups.append(current)
    return groups


def _ass_text_lines(words, active_index=None):
    lines = [[]]
    line_length = 0
    for index, item in enumerate(words):
        word = _ass_escape_text(item["word"])
        if lines[-1] and line_length + 1 + len(word) > ASS_MAX_LINE_CHARS and len(lines) < 2:
            lines.append([])
            line_length = 0
        if lines[-1]:
            lines[-1].append(" ")
            line_length += 1
        if active_index == index:
            lines[-1].append(r"{\c" + ASS_ORANGE + "}" + word + r"{\c" + ASS_WHITE + "}")
        else:
            lines[-1].append(word)
        line_length += len(word)
    return r"\N".join("".join(line) for line in lines)


def _ass_phrase_groups(text):
    words = [{"word": word} for word in str(text).split() if word]
    groups, current, line_length, lines = [], [], 0, 1
    for item in words:
        token_length = len(_ass_escape_text(item["word"]))
        next_lines = lines + int(bool(current) and line_length + 1 + token_length > ASS_MAX_LINE_CHARS)
        if current and next_lines > 2:
            groups.append(current)
            current, line_length, lines = [item], token_length, 1
        else:
            current.append(item)
            if len(current) == 1 or line_length + 1 + token_length > ASS_MAX_LINE_CHARS:
                lines += int(len(current) > 1)
                line_length = token_length
            else:
                line_length += 1 + token_length
    if current:
        groups.append(current)
    return groups


def _ass_text(segment, width=ASS_PLAY_RES_X, height=ASS_PLAY_RES_Y):
    """Create a TikTok-style ASS track; old phrase-only data stays renderable."""
    font_size = max(34, round(66 * height / ASS_PLAY_RES_Y))
    outline = max(3, round(5 * height / ASS_PLAY_RES_Y))
    margin_x = round(150 * width / ASS_PLAY_RES_X)
    margin_y = round(360 * height / ASS_PLAY_RES_Y)
    dialogue = []
    for caption in segment.subtitle_data or []:
        words = caption.get("words") or []
        groups = _ass_word_groups(words) if words else []
        if groups:
            for group in groups:
                for index, word in enumerate(group):
                    start = word["start"]
                    end = group[index + 1]["start"] if index + 1 < len(group) else word["end"]
                    if end <= start:
                        end = word["end"]
                    if end <= start:
                        continue
                    dialogue.append(
                        "Dialogue: 0,"
                        + _ass_time(start) + "," + _ass_time(end)
                        + ",Default,,0,0,0,,"
                        + _ass_text_lines(group, active_index=index)
                    )
            continue

        # Older projects contain only phrase start/end/text. Render those as
        # static, two-line captions instead of failing or inventing word timing.
        try:
            start, end = float(caption["start"]), float(caption["end"])
        except (KeyError, TypeError, ValueError):
            continue
        if end <= start:
            continue
        phrase_groups = _ass_phrase_groups(caption.get("text", ""))
        if not phrase_groups:
            continue
        if len(phrase_groups) == 1:
            dialogue.append(
                "Dialogue: 0,"
                + _ass_time(start) + "," + _ass_time(end)
                + ",Default,,0,0,0,,"
                + _ass_text_lines(phrase_groups[0])
            )
        else:
            total_chars = sum(len(item["word"]) for group in phrase_groups for item in group)
            elapsed = start
            for index, group in enumerate(phrase_groups):
                group_chars = sum(len(item["word"]) for item in group)
                group_end = end if index == len(phrase_groups) - 1 else (
                    elapsed + (end - start) * group_chars / max(1, total_chars)
                )
                dialogue.append(
                    "Dialogue: 0,"
                    + _ass_time(elapsed) + "," + _ass_time(group_end)
                    + ",Default,,0,0,0,,"
                    + _ass_text_lines(group)
                )
                elapsed = group_end

    header = [
        "[Script Info]",
        "ScriptType: v4.00+",
        f"PlayResX: {width}",
        f"PlayResY: {height}",
        "WrapStyle: 2",
        "ScaledBorderAndShadow: yes",
        "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
        "Style: Default,Arial," + str(font_size) + ",&H00FFFFFF,&H00FFFFFF,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1," + str(outline) + ",1,2," + str(margin_x) + "," + str(margin_x) + "," + str(margin_y) + ",1",
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    return "\n".join(header + dialogue) + "\n"


def _ass_text_from_words(words, width=ASS_PLAY_RES_X, height=ASS_PLAY_RES_Y):
    """Build one global ASS track from the AudioAsset's project-relative word timings."""
    class WordCaptions:
        subtitle_data = [{"words": words}]

    return _ass_text(WordCaptions(), width, height)


def validate_assembly_project(project):
    """Validate every database and media input before an assembly render starts."""
    asset = project.audio_asset
    if asset is None:
        raise ValueError("Sélectionne un audio dans la bibliothèque.")
    if asset.user_id != project.user_id or asset.source != AudioAsset.Source.XTTS:
        raise ValueError("Cet audio n'a pas de timings utilisables.")
    try:
        duration = float(asset.duration)
    except (TypeError, ValueError):
        raise ValueError("Cet audio n'a pas de timings utilisables.")
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError("Cet audio n'a pas de timings utilisables.")

    timings = asset.timings
    if not isinstance(timings, dict) or timings.get("version") != 1:
        raise ValueError("Cet audio n'a pas de timings utilisables.")
    words = timings.get("words")
    if words is None or words == []:
        raise ValueError("Impossible de générer les sous-titres (words manquants).")
    if not isinstance(words, list):
        raise ValueError("Cet audio n'a pas de timings utilisables.")
    previous_start = -1.0
    for word in words:
        if not isinstance(word, dict) or not isinstance(word.get("word"), str) or not word["word"].strip():
            raise ValueError("Cet audio n'a pas de timings utilisables.")
        try:
            start, end = float(word["start"]), float(word["end"])
        except (KeyError, TypeError, ValueError):
            raise ValueError("Cet audio n'a pas de timings utilisables.")
        if (not math.isfinite(start) or not math.isfinite(end) or start < 0 or end <= start
                or end > duration + 0.1 or start < previous_start):
            raise ValueError("Cet audio n'a pas de timings utilisables.")
        previous_start = start

    boundaries = timings.get("scene_boundaries")
    if boundaries is None or boundaries == []:
        raise ValueError("Impossible de générer la séquence (boundaries manquantes).")
    if not isinstance(boundaries, list) or len(boundaries) > 30:
        raise ValueError("Cet audio n'a pas de timings utilisables.")
    normalized_boundaries = []
    previous_end = 0.0
    for index, boundary in enumerate(boundaries):
        if not isinstance(boundary, dict) or type(boundary.get("scene_index")) is not int:
            raise ValueError("Cet audio n'a pas de timings utilisables.")
        try:
            start, end = float(boundary["start"]), float(boundary["end"])
        except (KeyError, TypeError, ValueError):
            raise ValueError("Cet audio n'a pas de timings utilisables.")
        if (boundary["scene_index"] != index or not math.isfinite(start) or not math.isfinite(end)
                or start < 0 or end <= start or end > duration + 0.1
                or abs(start - previous_end) > 0.1):
            raise ValueError("Cet audio n'a pas de timings utilisables.")
        normalized_boundaries.append({"scene_index": index, "start": start, "end": end})
        previous_end = end
    if abs(normalized_boundaries[0]["start"]) > 0.1 or abs(previous_end - duration) > 0.1:
        raise ValueError("Cet audio n'a pas de timings utilisables.")

    segments_by_position = {}
    for segment in project.segments.filter(position__gte=0, position__lt=len(normalized_boundaries)).order_by("position", "id"):
        segments_by_position.setdefault(segment.position, segment)
    visuals = []
    missing_positions = []
    for position in range(len(normalized_boundaries)):
        segment = segments_by_position.get(position)
        if segment is None:
            missing_positions.append(position)
            continue
        has_image = bool(segment.image and segment.image.name)
        has_video = bool(segment.video_clip and segment.video_clip.name)
        if has_image and has_video:
            raise ValueError(
                f"L’emplacement {position + 1} contient une image et une vidéo. Choisis un seul visuel."
            )
        if not has_image and not has_video:
            missing_positions.append(position)
            continue

        if has_image:
            image_path = Path(segment.image.path)
            if not image_path.is_file():
                missing_positions.append(position)
                continue
            visuals.append({
                "kind": "image", "path": image_path, "duration": None,
                "assembly_motion": segment.assembly_motion,
                "assembly_animation_settings": _assembly_animation_settings_for_segment(segment),
            })
            continue

        video_path = Path(segment.video_clip.path)
        if not video_path.is_file():
            missing_positions.append(position)
            continue
        if video_path.suffix.lower() not in {".mp4", ".mov"}:
            raise ValueError(f"La vidéo de l’emplacement {position + 1} doit être au format MP4 ou MOV.")
        try:
            metadata = _probe_clip(video_path)
        except RuntimeError as exc:
            raise ValueError(f"La vidéo de l’emplacement {position + 1} est illisible : {exc}") from exc
        visuals.append({"kind": "video", "path": video_path, "duration": metadata["duration"]})

    if missing_positions:
        count = len(missing_positions)
        noun = "visuel" if count == 1 else "visuels"
        raise ValueError(f"Il manque {count} {noun} pour les {len(normalized_boundaries)} scènes. Ajoute une image ou une vidéo.")

    if not asset.audio_file or not Path(asset.audio_file.path).is_file():
        raise ValueError("Le fichier audio sélectionné est introuvable dans la bibliothèque.")
    if project.background_track and project.background_track.user_id != project.user_id:
        raise ValueError("La musique sélectionnée n'appartient pas à ce compte.")
    return {
        "asset": asset,
        "audio_path": Path(asset.audio_file.path),
        "duration": duration,
        "words": words,
        "boundaries": normalized_boundaries,
        "visuals": visuals,
    }


def _subtitle_filter(subtitle_file):
    return "subtitles=filename=" + Path(subtitle_file).name

_REMBG_SESSION = None
_REMBG_SESSION_LOCK = threading.Lock()


def _get_rembg_session():
    """Load the small local U2NetP subject-segmentation model only when needed."""
    global _REMBG_SESSION
    if _REMBG_SESSION is None:
        with _REMBG_SESSION_LOCK:
            if _REMBG_SESSION is None:
                try:
                    from rembg import new_session
                except ImportError as exc:
                    raise RuntimeError(
                        "La profondeur douce nécessite le module local rembg. "
                        "Installe-le avec : python -m pip install -r requirements-parallax.txt"
                    ) from exc
                try:
                    _REMBG_SESSION = new_session("u2netp")
                except Exception as exc:
                    raise RuntimeError(
                        f"Impossible de charger le modèle local de détourage U2NetP : {exc}"
                    ) from exc
    return _REMBG_SESSION


def _foreground_for_image(image_path):
    """Return a cached transparent subject cutout; model inference runs once per image."""
    from PIL import Image

    digest = hashlib.sha256()
    with open(image_path, "rb") as source_file:
        for block in iter(lambda: source_file.read(1024 * 1024), b""):
            digest.update(block)
    cache_dir = Path(settings.MEDIA_ROOT) / "parallax-cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    cutout_path = cache_dir / f"{digest.hexdigest()}-u2netp.png"
    if cutout_path.is_file() and cutout_path.stat().st_size > 1000:
        return Image.open(cutout_path).convert("RGBA")

    try:
        from rembg import remove
    except ImportError as exc:
        raise RuntimeError(
            "La profondeur douce nécessite le module local rembg. "
            "Installe-le avec : python -m pip install -r requirements-parallax.txt"
        ) from exc
    try:
        with Image.open(image_path) as source_image:
            cutout = remove(source_image.convert("RGB"), session=_get_rembg_session())
        cutout = cutout.convert("RGBA")
        temporary = cutout_path.with_name(cutout_path.stem + ".tmp.png")
        cutout.save(temporary, format="PNG")
        temporary.replace(cutout_path)
        return cutout
    except RuntimeError:
        raise
    except Exception as exc:
        raise RuntimeError(f"Le détourage local du sujet a échoué : {exc}") from exc


def _parallax_frames(image_path, width, height, duration, level="medium"):
    """Yield RGB frames with a crisp foreground drifting over a softly moving background."""
    from PIL import Image, ImageEnhance, ImageFilter, ImageOps

    try:
        foreground = _foreground_for_image(image_path)
    except Exception as exc:
        raise RuntimeError(f"Impossible de préparer la profondeur douce : {exc}") from exc

    # Overscan leaves room for the later zoom/rotation crop without exposing black edges.
    extra_x = max(24, round(width * 0.10))
    extra_y = max(24, round(height * 0.10))
    layer_size = (width + 2 * extra_x, height + 2 * extra_y)
    with Image.open(image_path) as source_image:
        background = ImageOps.fit(source_image.convert("RGB"), layer_size, method=Image.Resampling.LANCZOS)
    background = background.filter(ImageFilter.GaussianBlur(radius=max(8, round(min(width, height) * 0.018))))
    background = ImageEnhance.Brightness(background).enhance(0.82)
    foreground = ImageOps.fit(foreground, layer_size, method=Image.Resampling.LANCZOS)

    movement = {
        "slow": (0.10, 0.45),
        "medium": (0.18, 0.82),
        "fast": (0.25, 0.95),
    }.get(level, (0.18, 0.82))
    frames = max(1, round(max(0.1, duration) * 24))
    for index in range(frames):
        progress = index / max(1, frames - 1)
        bg_x = extra_x + round(extra_x * movement[0] * progress)
        bg_y = extra_y + round(extra_y * movement[0] * progress)
        fg_x = extra_x + round(extra_x * movement[1] * progress)
        fg_y = extra_y + round(extra_y * movement[1] * progress)
        bg_frame = background.crop((bg_x, bg_y, bg_x + width, bg_y + height))
        fg_frame = foreground.crop((fg_x, fg_y, fg_x + width, fg_y + height))
        bg_frame = bg_frame.convert("RGBA")
        bg_frame.alpha_composite(fg_frame.convert("RGBA"))
        yield bg_frame.convert("RGB").tobytes()


def _run_parallax_attempt(
    command, image_path, output_path, width, height, duration, folder,
    cancel_check=None, parallax_level="medium",
):
    error_log = folder / f"{output_path.stem}-ffmpeg.log"
    try:
        with error_log.open("wb") as log_stream:
            process = subprocess.Popen(command, cwd=folder, stdin=subprocess.PIPE, stderr=log_stream)
            timed_out = threading.Event()

            def kill_after_timeout():
                if process.poll() is None:
                    timed_out.set()
                    try:
                        process.kill()
                    except OSError:
                        pass

            timeout_timer = threading.Timer(FFMPEG_TIMEOUT_SECONDS, kill_after_timeout)
            timeout_timer.start()
            write_error = None
            try:
                for frame in _parallax_frames(image_path, width, height, duration, parallax_level):
                    if cancel_check and cancel_check():
                        raise JobCancelled("Traitement annulé à la demande de l’utilisateur.")
                    process.stdin.write(frame)
            except Exception as exc:
                write_error = exc
                _terminate_subprocess(process)
            finally:
                if process.stdin and not process.stdin.closed:
                    try:
                        process.stdin.close()
                    except OSError as exc:
                        if write_error is None:
                            write_error = exc
            try:
                return_code = process.wait(timeout=FFMPEG_TIMEOUT_SECONDS)
            except subprocess.TimeoutExpired as exc:
                _terminate_subprocess(process)
                timed_out.set()
                raise RuntimeError(
                    "FFmpeg a dépassé le délai de 10 minutes pendant le rendu par parallaxe."
                ) from exc
            finally:
                timeout_timer.cancel()
            if timed_out.is_set():
                raise RuntimeError(
                    "FFmpeg a dépassé le délai de 10 minutes pendant le rendu par parallaxe."
                )
        stderr = error_log.read_text(encoding="utf-8", errors="replace").strip()
    except OSError as exc:
        raise RuntimeError(f"Impossible de lancer FFmpeg pour la profondeur douce : {exc}") from exc
    if write_error and not isinstance(write_error, (BrokenPipeError, OSError)):
        raise write_error
    if return_code:
        if "No such filter" in stderr and "subtitles" in stderr:
            raise RuntimeError("FFmpeg ne prend pas en charge l’incrustation des sous-titres (filtre libass). Réinstalle FFmpeg avec le support libass.")
        detail = stderr.splitlines()[-1] if stderr else f"code de sortie {return_code}"
        raise _FFmpegCommandError(f"FFmpeg a échoué pendant le rendu par parallaxe : {detail}")
    if write_error:
        raise RuntimeError(f"Le flux vidéo par parallaxe a été interrompu : {write_error}")


def _render_parallax_clip(
    ffmpeg, image_path, audio_path, output_path, subtitle_file, width, height, duration, folder,
    animation_settings=None, cancel_check=None,
):
    """Encode parallax base frames, then apply the shared camera/light/grade/subtitle chain."""
    command = [
        ffmpeg, "-y", "-v", "error",
        "-f", "rawvideo", "-pixel_format", "rgb24", "-video_size", f"{width}x{height}",
        "-framerate", "24", "-i", "pipe:0", "-i", str(audio_path), "-t", f"{duration:.3f}",
    ]
    video_filters = _animation_filter_chain(animation_settings, width, height, duration, subtitle_file)
    if video_filters:
        command.extend(["-vf", ",".join(video_filters)])
    command.extend([
        *_video_encoder_args(ffmpeg, width, height),
        "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "128k", "-ar", "48000", "-ac", "2", "-shortest", str(output_path),
    ])

    try:
        parallax_level = (animation_settings or {}).get("effects", {}).get("parallax", {}).get("level", "medium")
        _run_parallax_attempt(
            command, image_path, output_path, width, height, duration, folder,
            cancel_check=cancel_check, parallax_level=parallax_level,
        )
    except _FFmpegCommandError:
        fallback = _software_fallback_command(command)
        if fallback is None:
            raise
        _remove_partial_output(command, folder)
        _run_parallax_attempt(
            fallback, image_path, output_path, width, height, duration, folder,
            cancel_check=cancel_check, parallax_level=parallax_level,
        )


def _ffprobe_path():
    ffmpeg = Path(_ffmpeg_path())
    sibling = ffmpeg.with_name("ffprobe" + ffmpeg.suffix)
    path = str(sibling) if sibling.is_file() else shutil.which("ffprobe")
    if not path:
        raise RuntimeError("ffprobe est introuvable. Réinstalle FFmpeg avec ffprobe dans le PATH.")
    return path


def _parse_fps(value):
    if not value:
        return None
    try:
        if "/" in value:
            numerator, denominator = value.split("/", 1)
            denominator = float(denominator)
            return float(numerator) / denominator if denominator else None
        return float(value)
    except (TypeError, ValueError, ZeroDivisionError):
        return None


def _probe_clip(path):
    started = time.perf_counter()
    try:
        result = subprocess.run(
            [_ffprobe_path(), "-v", "error", "-show_entries",
             "stream=codec_type,duration,width,height,codec_name,avg_frame_rate,r_frame_rate:format=duration",
             "-of", "json", str(path)],
            capture_output=True, text=True, check=False,
            timeout=FFPROBE_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        elapsed = time.perf_counter() - started
        print(f"[ffprobe] {Path(path).name} · timeout · durée={elapsed:.2f}s", flush=True)
        raise RuntimeError(
            f"ffprobe a dépassé le délai de 60 secondes pendant l’analyse du clip « {Path(path).name} »."
        ) from exc
    elapsed = time.perf_counter() - started
    print(f"[ffprobe] {Path(path).name} · durée de l’analyse={elapsed:.2f}s", flush=True)
    if result.returncode:
        detail = (result.stderr or "").strip().splitlines()
        raise RuntimeError(f"Le clip « {Path(path).name} » est illisible : {detail[-1] if detail else 'format invalide'}")
    try:
        info = json.loads(result.stdout)
        streams = info.get("streams", [])
        video_streams = [stream for stream in streams if stream.get("codec_type") == "video"]
        if not video_streams:
            raise ValueError("aucune piste vidéo")
        video = video_streams[0]
        try:
            duration = float(video.get("duration"))
        except (TypeError, ValueError):
            duration = 0.0
        if duration <= 0:
            duration = float(info.get("format", {}).get("duration") or 0)
        if duration <= 0:
            raise ValueError("durée inconnue")
        has_audio = any(stream.get("codec_type") == "audio" for stream in streams)
        width = video.get("width")
        height = video.get("height")
        codec = video.get("codec_name") or "inconnu"
        fps_raw = video.get("avg_frame_rate")
        fps = _parse_fps(fps_raw)
        if fps is None:
            fps_raw = video.get("r_frame_rate")
            fps = _parse_fps(fps_raw)
        metadata = {
            "duration": duration,
            "has_audio": has_audio,
            "width": width,
            "height": height,
            "codec": codec,
            "fps": fps,
            "fps_raw": fps_raw,
            "probe_seconds": elapsed,
        }
        resolution = f"{width}x{height}" if width and height else "inconnue"
        fps_label = f"{fps:.3f}" if fps is not None else f"inconnu ({fps_raw or 'non fourni'})"
        print(
            f"[ffprobe] {Path(path).name} · résolution={resolution} · codec={codec} · "
            f"fps={fps_label} · durée vidéo={duration:.3f}s · audio={has_audio}",
            flush=True,
        )
        return metadata
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Impossible de lire les métadonnées du clip « {Path(path).name} » : {exc}") from exc


def render_video_assembly(project_id):
    """Normalize and concatenate imported clips, retaining their original audio."""
    close_old_connections()
    try:
        project = NarrationProject.objects.get(pk=project_id)
        cancel_check = cancellation_checker(project_id)
        check_job_cancellation(project_id)
        scenes = list(project.segments.order_by("position", "id"))
        if not scenes or any(not scene.video_clip or scene.image for scene in scenes):
            raise RuntimeError("Ajoute uniquement des clips vidéo avant de lancer l’assemblage.")
        ffmpeg = _ffmpeg_path()
        width, height = _dimensions(project.video_format)
        with tempfile.TemporaryDirectory(prefix=f"petit-studio-assembly-{project.pk}-") as temp:
            folder = Path(temp)
            clips = []
            total_duration = 0.0
            for index, scene in enumerate(scenes, start=1):
                check_job_cancellation(project_id)
                project.status_message = f"Préparation du clip {index}/{len(scenes)}"
                project.current_segment = index
                project.progress_percent = round(85 * (index - 1) / len(scenes))
                project.save(update_fields=["status_message", "current_segment", "progress_percent", "updated_at"])
                source = Path(scene.video_clip.path)
                clip_metadata = _probe_clip(source)
                duration = clip_metadata["duration"]
                has_audio = clip_metadata["has_audio"]
                total_duration += duration
                clip = folder / f"clip-{index:04}.mp4"
                command = [ffmpeg, "-y", "-v", "error", "-i", str(source)]
                if not has_audio:
                    command.extend([
                        "-f", "lavfi", "-t", f"{duration:.3f}", "-i",
                        "anullsrc=channel_layout=stereo:sample_rate=48000",
                    ])
                audio_input = "0:a:0" if has_audio else "1:a:0"
                filters = (
                    f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
                    f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black,fps=24,setsar=1"
                )
                command.extend([
                    "-map", "0:v:0", "-map", audio_input, "-vf", filters,
                    "-af", "aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo,apad",
                    "-t", f"{duration:.3f}", "-r", "24", *_video_encoder_args(ffmpeg, width, height),
                    "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k",
                    "-ar", "48000", "-ac", "2", str(clip),
                ])
                _run_ffmpeg(command, folder, f"normalisation du clip {index}", cancel_check=cancel_check)
                if not clip.is_file() or clip.stat().st_size < 1000:
                    raise RuntimeError(f"FFmpeg n’a pas produit le clip normalisé {index}.")
                clips.append(clip)
            concat_list = folder / "clips.txt"
            concat_list.write_text("".join(f"file '{clip.name}'\n" for clip in clips), encoding="utf-8")
            output = folder / "montage.mp4"
            project.status_message = "Assemblage des clips vidéo…"
            project.progress_percent = 92
            project.save(update_fields=["status_message", "progress_percent", "updated_at"])
            _run_ffmpeg(
                [ffmpeg, "-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", str(concat_list),
                 "-c", "copy", "-movflags", "+faststart", str(output)],
                folder, "assemblage des clips vidéo", cancel_check=cancel_check,
            )
            if not output.is_file() or output.stat().st_size < 1000:
                raise RuntimeError("FFmpeg n’a pas produit de fichier MP4 valide.")
            check_job_cancellation(project_id)
            project.refresh_from_db()
            if project.video_file:
                project.video_file.delete(save=False)
            with output.open("rb") as stream:
                project.video_file.save(f"montage-{project.pk}.mp4", File(stream), save=False)
            project.duration_seconds = total_duration
            project.status = NarrationProject.Status.READY
            project.status_message = f"Assemblage prêt · {len(scenes)} clip(s) · son d’origine conservé · {project.video_format}."
            project.error_message = ""
            project.progress_percent = 100
            project.current_segment = len(scenes)
            project.total_segments = len(scenes)
            project.finished_at = timezone.now()
            project.save(update_fields=[
                "video_file", "duration_seconds", "status", "status_message", "error_message",
                "progress_percent", "current_segment", "total_segments", "finished_at", "updated_at",
            ])
    except JobCancelled:
        mark_job_cancelled(project_id)
    except Exception as exc:
        NarrationProject.objects.filter(pk=project_id).update(
            status=NarrationProject.Status.ERROR,
            status_message="L’assemblage des clips a échoué.",
            error_message=str(exc)[:2000],
            queued_at=None,
            finished_at=timezone.now(),
        )
    finally:
        close_old_connections()






def _normalized_clip_filters(width, height, source_duration, target_duration, subtitle_file=None):
    """Normalize a visual clip like the existing Scenes renderer and pad shorter sources."""
    filters = [
        f"scale={width}:{height}:force_original_aspect_ratio=increase",
        f"crop={width}:{height}",
        "fps=24",
        "setsar=1",
    ]
    stop_duration = max(0.0, target_duration - source_duration)
    if stop_duration > 0.01:
        filters.append(f"tpad=stop_mode=clone:stop_duration={stop_duration:.3f}")
    if subtitle_file:
        filters.append(_subtitle_filter(subtitle_file))
    return filters


def _video_narration_command(
    ffmpeg, source, voice_path, output, width, height, video_duration, voice_duration, subtitle_file=None,
):
    """Build a clip render command that maps video only from the source and audio only from XTTS."""
    filters = _normalized_clip_filters(width, height, video_duration, voice_duration, subtitle_file)
    return [
        ffmpeg, "-y", "-v", "error",
        "-i", str(source), "-i", str(voice_path),
        "-map", "0:v:0", "-map", "1:a:0",
        "-vf", ",".join(filters),
        "-af", "aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo",
        "-t", f"{voice_duration:.3f}", "-r", "24",
        *_video_encoder_args(ffmpeg, width, height),
        "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k",
        "-ar", "48000", "-ac", "2", "-shortest", str(output),
    ]


def _music_mix_command(ffmpeg, video_path, music_path, output_path, volume, duration):
    """Build a final audio-only re-encode, preserving the completed video bitstream."""
    volume = int(volume)
    duration = float(duration)
    if not 0 <= volume <= 100:
        raise ValueError("Le volume de musique doit être compris entre 0 et 100.")
    if duration <= 0:
        raise ValueError("La durée de la vidéo doit être supérieure à zéro pour le mixage musical.")

    fade_duration = min(1.5, duration / 2)
    fade_out_start = max(0.0, duration - fade_duration)
    music_volume = volume / 100
    filter_complex = (
        "[0:a:0]aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo[voice];"
        f"[1:a:0]aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo,"
        f"volume={music_volume:.3f},afade=t=in:st=0:d={fade_duration:.3f},"
        f"afade=t=out:st={fade_out_start:.3f}:d={fade_duration:.3f}[music];"
        "[voice][music]amix=inputs=2:duration=first:dropout_transition=0:normalize=0[mixed]"
    )
    return [
        ffmpeg, "-y", "-v", "error",
        "-i", str(video_path),
        "-stream_loop", "-1", "-i", str(music_path),
        "-filter_complex", filter_complex,
        "-map", "0:v:0", "-map", "[mixed]",
        "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2",
        "-t", f"{duration:.3f}", "-shortest", "-movflags", "+faststart", str(output_path),
    ]


def mix_music_with_video(video_path, music_path, volume, duration, output_path, cancel_check=None):
    """Mix a looped, faded music bed under the finished narration without re-encoding video."""
    ffmpeg = _ffmpeg_path()
    output_path = Path(output_path)
    command = _music_mix_command(ffmpeg, video_path, music_path, output_path, volume, duration)
    _run_ffmpeg(command, output_path.parent, "mixage de la musique de fond", cancel_check=cancel_check)
    if not output_path.is_file() or output_path.stat().st_size < 1000:
        raise RuntimeError("FFmpeg n’a pas produit la vidéo avec musique de fond.")
    return output_path


def render_project_assembly(project_id):
    """Render silent boundary visuals, concatenate them, then add library audio and global ASS."""
    close_old_connections()
    try:
        project = NarrationProject.objects.select_related("audio_asset", "background_track").get(pk=project_id)
        if project.mode != NarrationProject.Mode.ASSEMBLY:
            raise ValueError("Le projet n’est plus en mode Assemblage.")
        cancel_check = cancellation_checker(project_id)
        check_job_cancellation(project_id)

        # All validation occurs before creating the FFmpeg command or output files.
        inputs = validate_assembly_project(project)
        boundaries = inputs["boundaries"]
        visuals = inputs["visuals"]
        width, height = _dimensions(project.video_format)
        ffmpeg = _ffmpeg_path()
        project.status_message = "Préparation de l’assemblage audio…"
        project.total_segments = len(boundaries)
        project.current_segment = 0
        project.progress_percent = 5
        project.save(update_fields=["status_message", "total_segments", "current_segment", "progress_percent", "updated_at"])

        with tempfile.TemporaryDirectory(prefix=f"petit-studio-audio-assembly-{project.pk}-") as temp:
            folder = Path(temp)
            subtitle_file = None
            if project.subtitles_enabled:
                subtitle_file = folder / "assembly.ass"
                subtitle_file.write_text(
                    _ass_text_from_words(inputs["words"], width, height), encoding="utf-8",
                )

            silent_clips = []
            intermediate_commands = []
            force_software_encoder = False
            for index, (visual, boundary) in enumerate(zip(visuals, boundaries), start=1):
                check_job_cancellation(project_id)
                clip_duration = boundary["end"] - boundary["start"]
                visual_path = visual["path"]
                silent_clip = folder / f"visual-{index:04}.mp4"
                filters = None
                if visual["kind"] == "image":
                    animation_settings = (
                        visual.get("assembly_animation_settings")
                        or default_assembly_animation_settings()
                    )
                    filters = _assembly_animation_filters(
                        animation_settings, width, height, clip_duration,
                    )
                if filters is None:
                    filters = _normalized_clip_filters(
                        width,
                        height,
                        visual["duration"] if visual["kind"] == "video" else clip_duration,
                        clip_duration,
                    )
                command = [ffmpeg, "-y", "-v", "error"]
                if visual["kind"] == "image":
                    command.extend([
                        "-loop", "1", "-framerate", "24", "-t", f"{clip_duration:.3f}",
                        "-i", str(visual_path),
                    ])
                else:
                    command.extend(["-i", str(visual_path), "-map", "0:v:0"])
                command.extend([
                    "-vf", ",".join(filters), "-an", "-t", f"{clip_duration:.3f}", "-r", "24",
                    *_video_encoder_args(ffmpeg, width, height), "-pix_fmt", "yuv420p", str(silent_clip),
                ])
                if force_software_encoder:
                    command = _software_fallback_command(command) or command
                project.status_message = f"Préparation du visuel silencieux {index}/{len(visuals)}…"
                project.current_segment = index
                project.progress_percent = 5 + round(50 * index / len(visuals))
                project.save(update_fields=["status_message", "current_segment", "progress_percent", "updated_at"])
                fallback_used = _run_ffmpeg(
                    command, folder, f"rendu silencieux du visuel {index}", cancel_check=cancel_check,
                )
                if fallback_used and not force_software_encoder:
                    # Keep every file passed to concat -c copy on the same encoder/profile.
                    for prior_command in intermediate_commands:
                        prior_fallback = _software_fallback_command(prior_command)
                        if prior_fallback:
                            _remove_partial_output(prior_command, folder)
                            _run_ffmpeg(
                                prior_fallback, folder, "réencodage homogène d’un visuel", cancel_check=cancel_check,
                            )
                    force_software_encoder = True
                intermediate_commands.append(command)
                if not silent_clip.is_file() or silent_clip.stat().st_size < 1000:
                    raise RuntimeError(f"FFmpeg n’a pas produit le visuel silencieux {index}.")
                silent_clips.append(silent_clip)

            concat_list = folder / "assembly-visuals.txt"
            concat_list.write_text("".join(f"file '{clip.name}'\n" for clip in silent_clips), encoding="utf-8")
            silent_video = folder / "assembly-visuals.mp4"
            project.status_message = "Concaténation des visuels silencieux…"
            project.progress_percent = 65
            project.save(update_fields=["status_message", "progress_percent", "updated_at"])
            _run_ffmpeg(
                [ffmpeg, "-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", str(concat_list),
                 "-c", "copy", "-movflags", "+faststart", str(silent_video)],
                folder, "concaténation des visuels silencieux", cancel_check=cancel_check,
            )
            if not silent_video.is_file() or silent_video.stat().st_size < 1000:
                raise RuntimeError("FFmpeg n’a pas produit la vidéo silencieuse concaténée.")

            output = folder / "assembly.mp4"
            final_command = [
                ffmpeg, "-y", "-v", "error", "-i", str(silent_video), "-i", str(inputs["audio_path"]),
                "-map", "0:v:0", "-map", "1:a:0",
            ]
            if subtitle_file:
                final_command.extend(["-vf", _subtitle_filter(subtitle_file)])
            final_command.extend([
                "-af", "aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo",
                "-t", f"{inputs['duration']:.3f}", "-r", "24",
                *_video_encoder_args(ffmpeg, width, height),
                "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k",
                "-ar", "48000", "-ac", "2", "-shortest", "-movflags", "+faststart", str(output),
            ])
            project.status_message = "Encodage final avec l’audio et les sous-titres…"
            project.progress_percent = 80
            project.save(update_fields=["status_message", "progress_percent", "updated_at"])
            _run_ffmpeg(final_command, folder, "encodage final de l’assemblage", cancel_check=cancel_check)
            if not output.is_file() or output.stat().st_size < 1000:
                raise RuntimeError("FFmpeg n’a pas produit de vidéo d’assemblage valide.")

            if project.background_track_id:
                project.status_message = "Mixage de la musique de fond…"
                project.progress_percent = 90
                project.save(update_fields=["status_message", "progress_percent", "updated_at"])
                mixed_output = folder / "assembly-music.mp4"
                output = mix_music_with_video(
                    output,
                    project.background_track.audio_file.path,
                    project.music_volume,
                    inputs["duration"],
                    mixed_output,
                    cancel_check=cancel_check,
                )

            check_job_cancellation(project_id)
            project.refresh_from_db()
            if project.video_file:
                project.video_file.delete(save=False)
            with output.open("rb") as stream:
                project.video_file.save(f"assemblage-{project.pk}.mp4", File(stream), save=False)
            project.duration_seconds = inputs["duration"]
            project.status = NarrationProject.Status.READY
            project.status_message = (
                f"Assemblage prêt · {len(boundaries)} scène(s) · audio de la bibliothèque · {project.video_format}."
            )
            project.error_message = ""
            project.progress_percent = 100
            project.current_segment = len(boundaries)
            project.total_segments = len(boundaries)
            project.finished_at = timezone.now()
            project.save(update_fields=[
                "video_file", "duration_seconds", "status", "status_message", "error_message",
                "progress_percent", "current_segment", "total_segments", "finished_at", "updated_at",
            ])
    except JobCancelled:
        mark_job_cancelled(project_id)
    except Exception as exc:
        NarrationProject.objects.filter(pk=project_id).update(
            status=NarrationProject.Status.ERROR,
            status_message="Le rendu Assemblage a échoué.",
            error_message=str(exc)[:2000],
            queued_at=None,
            finished_at=timezone.now(),
        )
    finally:
        close_old_connections()


def render_video_narration(project_id):
    """Replace imported clip audio with XTTS WAVs and burn WhisperX-aligned ASS captions."""
    close_old_connections()
    try:
        project = NarrationProject.objects.get(pk=project_id)
        cancel_check = cancellation_checker(project_id)
        check_job_cancellation(project_id)
        scenes = list(project.segments.order_by("position", "id"))
        if not scenes or any(not scene.video_clip or scene.image for scene in scenes):
            raise RuntimeError("Ajoute uniquement des clips vidéo pour créer une narration vidéo.")
        if any(not scene.audio or not Path(scene.audio.path).is_file() for scene in scenes):
            raise RuntimeError("La voix XTTS de certaines scènes n’est pas prête. Relance la préparation.")
        if any(not scene.text.strip() for scene in scenes):
            raise RuntimeError("Ajoute un texte à chaque clip avant de lancer la narration vidéo.")

        ffmpeg = _ffmpeg_path()
        width, height = _dimensions(project.video_format)
        with tempfile.TemporaryDirectory(prefix=f"petit-studio-video-narration-{project.pk}-") as temp:
            folder = Path(temp)
            clips = []
            total_voice_duration = 0.0
            for index, scene in enumerate(scenes, start=1):
                check_job_cancellation(project_id)
                project.status_message = f"Narration vidéo · scène {index}/{len(scenes)}"
                project.current_segment = index
                project.progress_percent = 5 + round(85 * (index - 1) / len(scenes))
                project.save(update_fields=["status_message", "current_segment", "progress_percent", "updated_at"])

                source = Path(scene.video_clip.path)
                voice_path = Path(scene.audio.path)
                clip_metadata = _probe_clip(source)
                video_duration = clip_metadata["duration"]
                print(
                    f"[Narration vidéo] Projet {project_id} · scène {index}/{len(scenes)} · "
                    f"ffprobe={clip_metadata['probe_seconds']:.2f}s",
                    flush=True,
                )
                voice_duration = wav_duration(voice_path)
                total_voice_duration += voice_duration
                subtitle_file = None
                if project.subtitles_enabled:
                    subtitle_file = folder / f"scene-{index:04}.ass"
                    subtitle_content = _ass_text(scene, width, height)
                    if "Dialogue:" not in subtitle_content:
                        raise RuntimeError(f"Aucun sous-titre synchronisé n’est disponible pour la scène {index}.")
                    subtitle_file.write_text(subtitle_content, encoding="utf-8")

                clip = folder / f"scene-{index:04}.mp4"
                command = _video_narration_command(
                    ffmpeg, source, voice_path, clip, width, height,
                    video_duration, voice_duration, subtitle_file,
                )
                encode_started = time.perf_counter()
                _run_ffmpeg(command, folder, f"narration vidéo de la scène {index}", cancel_check=cancel_check)
                print(
                    f"[Narration vidéo] Projet {project_id} · scène {index}/{len(scenes)} · "
                    f"encodage terminé en {time.perf_counter() - encode_started:.2f}s",
                    flush=True,
                )
                if not clip.is_file() or clip.stat().st_size < 1000:
                    raise RuntimeError(f"FFmpeg n’a pas produit la vidéo narrée de la scène {index}.")
                clips.append(clip)

            concat_list = folder / "scenes.txt"
            concat_list.write_text("".join(f"file '{clip.name}'\n" for clip in clips), encoding="utf-8")
            output = folder / "narration-video.mp4"
            project.status_message = "Assemblage de la narration vidéo…"
            project.progress_percent = 93
            project.save(update_fields=["status_message", "progress_percent", "updated_at"])
            concat_started = time.perf_counter()
            _run_ffmpeg(
                [ffmpeg, "-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", str(concat_list),
                 "-c", "copy", "-movflags", "+faststart", str(output)],
                folder, "assemblage de la narration vidéo", cancel_check=cancel_check,
            )
            print(
                f"[Narration vidéo] Projet {project_id} · concaténation finale en "
                f"{time.perf_counter() - concat_started:.2f}s",
                flush=True,
            )
            if not output.is_file() or output.stat().st_size < 1000:
                raise RuntimeError("FFmpeg n’a pas produit de fichier MP4 valide.")
            if project.background_track_id:
                mixed_output = folder / "narration-video-musique.mp4"
                output = mix_music_with_video(
                    output, project.background_track.audio_file.path, project.music_volume,
                    total_voice_duration, mixed_output, cancel_check=cancel_check,
                )
            check_job_cancellation(project_id)
            project.refresh_from_db()
            if project.video_file:
                project.video_file.delete(save=False)
            with output.open("rb") as stream:
                project.video_file.save(f"narration-video-{project.pk}.mp4", File(stream), save=False)
            project.duration_seconds = total_voice_duration
            project.status = NarrationProject.Status.READY
            project.status_message = f"Narration vidéo prête · {len(scenes)} clip(s) · {project.video_format}."
            project.error_message = ""
            project.progress_percent = 100
            project.current_segment = len(scenes)
            project.total_segments = len(scenes)
            project.finished_at = timezone.now()
            project.save(update_fields=[
                "video_file", "duration_seconds", "status", "status_message", "error_message",
                "progress_percent", "current_segment", "total_segments", "finished_at", "updated_at",
            ])
    except JobCancelled:
        mark_job_cancelled(project_id)
    except Exception as exc:
        NarrationProject.objects.filter(pk=project_id).update(
            status=NarrationProject.Status.ERROR,
            status_message="La narration vidéo a échoué.",
            error_message=str(exc)[:2000],
            queued_at=None,
            finished_at=timezone.now(),
        )
    finally:
        close_old_connections()

def render_project_video(project_id):
    """Render each image/audio scene, optionally burn aligned captions, then concatenate MP4 clips."""
    close_old_connections()
    try:
        project = NarrationProject.objects.get(pk=project_id)
        cancel_check = cancellation_checker(project_id)
        check_job_cancellation(project_id)
        scenes = list(project.segments.order_by("position", "id"))
        if not scenes or any(not scene.image for scene in scenes):
            raise RuntimeError("Ajoute une image à chaque scène avant de générer la vidéo.")
        if any(not scene.audio or not Path(scene.audio.path).is_file() for scene in scenes):
            raise RuntimeError("La narration de certaines scènes n’est pas prête. Relance la préparation.")

        ffmpeg = _ffmpeg_path()
        width, height = _dimensions(project.video_format)
        total_duration = sum(max(0.1, float(scene.duration_seconds)) for scene in scenes)
        with tempfile.TemporaryDirectory(prefix=f"petit-studio-video-{project.pk}-") as temp:
            folder = Path(temp)
            clips = []
            for index, scene in enumerate(scenes, start=1):
                check_job_cancellation(project_id)
                project.status_message = f"Rendu vidéo · scène {index}/{len(scenes)}"
                project.current_segment = index
                project.progress_percent = 5 + round(85 * (index - 1) / len(scenes))
                project.save(update_fields=["status_message", "current_segment", "progress_percent", "updated_at"])

                clip = folder / f"scene-{index:04}.mp4"
                duration = max(0.1, float(scene.duration_seconds))
                subtitle_file = None
                if project.subtitles_enabled:
                    subtitle_file = folder / f"scene-{index:04}.ass"
                    subtitle_content = _ass_text(scene, width, height)
                    if "Dialogue:" not in subtitle_content:
                        raise RuntimeError(f"Aucun sous-titre synchronisé n’est disponible pour la scène {index}.")
                    subtitle_file.write_text(subtitle_content, encoding="utf-8")

                animation_settings = scene.animation_settings or {}
                effects = animation_settings.get("effects", {})
                if effects.get("parallax", {}).get("enabled") or scene.motion == NarrationSegment.Motion.PARALLAX:
                    project.status_message = f"Rendu vidéo · scène {index}/{len(scenes)} · profondeur douce"
                    project.save(update_fields=["status_message", "updated_at"])
                    _render_parallax_clip(
                        ffmpeg, scene.image.path, scene.audio.path, clip, subtitle_file,
                        width, height, duration, folder, animation_settings=animation_settings,
                        cancel_check=cancel_check,
                    )
                else:
                    filters = _animation_filter_chain(animation_settings, width, height, duration, subtitle_file)
                    command = [
                        ffmpeg, "-y", "-v", "error", "-loop", "1", "-i", str(scene.image.path),
                        "-i", str(scene.audio.path), "-t", f"{duration:.3f}", "-vf", ",".join(filters),
                        "-r", "24", *_video_encoder_args(ffmpeg, width, height),
                        "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k", "-ar", "48000",
                        "-ac", "2", "-shortest", str(clip),
                    ]
                    _run_ffmpeg(command, folder, f"rendu de la scène {index}", cancel_check=cancel_check)
                if not clip.is_file() or clip.stat().st_size < 1000:
                    raise RuntimeError(f"FFmpeg n’a pas produit la vidéo de la scène {index}.")
                clips.append(clip)

            concat_list = folder / "scenes.txt"
            concat_list.write_text("".join(f"file '{clip.name}'\n" for clip in clips), encoding="utf-8")
            output = folder / "narration.mp4"
            project.status_message = "Assemblage du fichier MP4…"
            project.progress_percent = 93
            project.save(update_fields=["status_message", "progress_percent", "updated_at"])
            _run_ffmpeg(
                [ffmpeg, "-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", str(concat_list),
                 "-c", "copy", "-movflags", "+faststart", str(output)],
                folder, "assemblage des scènes", cancel_check=cancel_check,
            )
            if not output.is_file() or output.stat().st_size < 1000:
                raise RuntimeError("FFmpeg n’a pas produit de fichier MP4 valide.")
            if project.background_track_id:
                mixed_output = folder / "narration-musique.mp4"
                output = mix_music_with_video(
                    output, project.background_track.audio_file.path, project.music_volume,
                    total_duration, mixed_output, cancel_check=cancel_check,
                )
            check_job_cancellation(project_id)
            project.refresh_from_db()
            if project.video_file:
                project.video_file.delete(save=False)
            with output.open("rb") as stream:
                project.video_file.save(f"narration-{project.pk}.mp4", File(stream), save=False)
            project.status = NarrationProject.Status.READY
            project.status_message = f"Vidéo MP4 prête · {len(scenes)} scène(s) · {project.video_format}."
            project.error_message = ""
            project.progress_percent = 100
            project.current_segment = len(scenes)
            project.finished_at = timezone.now()
            project.save(update_fields=[
                "video_file", "status", "status_message", "error_message", "progress_percent",
                "current_segment", "finished_at", "updated_at",
            ])
    except JobCancelled:
        mark_job_cancelled(project_id)
    except Exception as exc:
        NarrationProject.objects.filter(pk=project_id).update(
            status=NarrationProject.Status.ERROR,
            status_message="Le rendu de la vidéo a échoué.",
            error_message=str(exc)[:2000],
            finished_at=timezone.now(),
        )
    finally:
        close_old_connections()
