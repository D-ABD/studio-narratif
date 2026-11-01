"""Local FFmpeg rendering for narrated still-image scenes."""
import hashlib
import shutil
import subprocess
import threading
import tempfile
from pathlib import Path

from django.conf import settings
from django.core.files import File
from django.db import close_old_connections
from django.utils import timezone

from .models import NarrationProject, NarrationSegment
from .services import _ffmpeg_path, srt_text


def _dimensions(video_format):
    return (720, 1280) if video_format == NarrationProject.VideoFormat.VERTICAL else (1280, 720)


def _movement_filters(motion, width, height, duration):
    """Create restrained camera motion; the source image and characters stay unchanged."""
    if motion == "still":
        return [
            f"scale={width}:{height}:force_original_aspect_ratio=decrease",
            f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black",
            "fps=24",
        ]
    frames = max(1, round(max(0.1, duration) * 24))
    progress = f"min(on/{frames},1)"
    base = [f"scale={width}:{height}:force_original_aspect_ratio=increase", f"crop={width}:{height}"]
    if motion == "zoom_in":
        return base + [f"zoompan=z='min(zoom+0.00010,1.08)':d=1:s={width}x{height}:fps=24"]
    if motion == "zoom_out":
        return base + [f"zoompan=z='if(eq(on,1),1.08,max(zoom-0.00010,1.0))':d=1:s={width}x{height}:fps=24"]
    if motion == "pan_h":
        return base + [
            f"zoompan=z='1.08':x='(iw-iw/zoom)*{progress}':y='(ih-ih/zoom)/2':d=1:s={width}x{height}:fps=24"
        ]
    if motion == "pan_v":
        return base + [
            f"zoompan=z='1.08':x='(iw-iw/zoom)/2':y='(ih-ih/zoom)*{progress}':d=1:s={width}x{height}:fps=24"
        ]
    if motion == "zoom_pan":
        return base + [
            f"zoompan=z='1+0.08*{progress}':x='(iw-iw/zoom)*{progress}':y='(ih-ih/zoom)*{progress}':d=1:s={width}x{height}:fps=24"
        ]
    raise RuntimeError("Le mouvement choisi pour une scène n’est pas reconnu.")


def _run_ffmpeg(command, cwd, operation):
    try:
        result = subprocess.run(command, cwd=cwd, capture_output=True, text=True, check=False)
    except OSError as exc:
        raise RuntimeError(f"Impossible de lancer FFmpeg pour {operation} : {exc}") from exc
    if result.returncode:
        detail = (result.stderr or "").strip().splitlines()
        message = detail[-1] if detail else f"code de sortie {result.returncode}"
        if "No such filter" in (result.stderr or "") and "subtitles" in (result.stderr or ""):
            raise RuntimeError("FFmpeg ne prend pas en charge l’incrustation des sous-titres (filtre libass). Réinstalle FFmpeg avec le support libass.")
        raise RuntimeError(f"FFmpeg a échoué pendant {operation} : {message}")


def _subtitle_filter(subtitle_file):
    return (
        "subtitles=filename=" + subtitle_file.name
        + ":force_style='FontName=Arial,FontSize=24,PrimaryColour=&H00FFFFFF,"
        + "BackColour=&H99000000,BorderStyle=3,Outline=0,Shadow=0,Alignment=2,MarginV=55'"
    )


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


def _parallax_frames(image_path, width, height, duration):
    """Yield RGB frames with a crisp foreground drifting over a softly moving background."""
    from PIL import Image, ImageEnhance, ImageFilter, ImageOps

    try:
        foreground = _foreground_for_image(image_path)
    except Exception as exc:
        raise RuntimeError(f"Impossible de préparer la profondeur douce : {exc}") from exc

    extra_x = max(12, round(width * 0.04))
    extra_y = max(12, round(height * 0.04))
    layer_size = (width + 2 * extra_x, height + 2 * extra_y)
    with Image.open(image_path) as source_image:
        background = ImageOps.fit(source_image.convert("RGB"), layer_size, method=Image.Resampling.LANCZOS)
    background = background.filter(ImageFilter.GaussianBlur(radius=max(8, round(min(width, height) * 0.018))))
    background = ImageEnhance.Brightness(background).enhance(0.82)
    foreground = ImageOps.fit(foreground, layer_size, method=Image.Resampling.LANCZOS)

    frames = max(1, round(max(0.1, duration) * 24))
    for index in range(frames):
        progress = index / max(1, frames - 1)
        bg_x = extra_x + round(2 * extra_x * 0.18 * progress)
        bg_y = extra_y + round(2 * extra_y * 0.18 * progress)
        fg_x = extra_x + round(2 * extra_x * 0.82 * progress)
        fg_y = extra_y + round(2 * extra_y * 0.82 * progress)
        bg_frame = background.crop((bg_x, bg_y, bg_x + width, bg_y + height))
        fg_frame = foreground.crop((fg_x, fg_y, fg_x + width, fg_y + height))
        bg_frame = bg_frame.convert("RGBA")
        bg_frame.alpha_composite(fg_frame.convert("RGBA"))
        yield bg_frame.convert("RGB").tobytes()


def _render_parallax_clip(ffmpeg, image_path, audio_path, output_path, subtitle_file, width, height, duration, folder):
    """Encode parallax frames locally; audio and optional aligned subtitles stay on the usual path."""
    command = [
        ffmpeg, "-y", "-v", "error",
        "-f", "rawvideo", "-pixel_format", "rgb24", "-video_size", f"{width}x{height}",
        "-framerate", "24", "-i", "pipe:0", "-i", str(audio_path), "-t", f"{duration:.3f}",
    ]
    if subtitle_file:
        command.extend(["-vf", _subtitle_filter(subtitle_file)])
    command.extend([
        "-c:v", "libx264", "-preset", "ultrafast", "-crf", "24", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "128k", "-ar", "48000", "-ac", "2", "-shortest", str(output_path),
    ])
    error_log = folder / f"{output_path.stem}-ffmpeg.log"
    try:
        with error_log.open("wb") as log_stream:
            process = subprocess.Popen(command, cwd=folder, stdin=subprocess.PIPE, stderr=log_stream)
            write_error = None
            try:
                for frame in _parallax_frames(image_path, width, height, duration):
                    process.stdin.write(frame)
            except Exception as exc:
                write_error = exc
                try:
                    process.terminate()
                except OSError:
                    pass
            finally:
                if process.stdin and not process.stdin.closed:
                    process.stdin.close()
            return_code = process.wait()
        stderr = error_log.read_text(encoding="utf-8", errors="replace").strip()
    except OSError as exc:
        raise RuntimeError(f"Impossible de lancer FFmpeg pour la profondeur douce : {exc}") from exc
    if write_error and not isinstance(write_error, (BrokenPipeError, OSError)):
        raise write_error
    if return_code:
        if "No such filter" in stderr and "subtitles" in stderr:
            raise RuntimeError("FFmpeg ne prend pas en charge l’incrustation des sous-titres (filtre libass). Réinstalle FFmpeg avec le support libass.")
        detail = stderr.splitlines()[-1] if stderr else f"code de sortie {return_code}"
        raise RuntimeError(f"FFmpeg a échoué pendant le rendu par parallaxe : {detail}")
    if write_error:
        raise RuntimeError(f"Le flux vidéo par parallaxe a été interrompu : {write_error}")


def render_project_video(project_id):
    """Render each image/audio scene, optionally burn aligned captions, then concatenate MP4 clips."""
    close_old_connections()
    try:
        project = NarrationProject.objects.get(pk=project_id)
        scenes = list(project.segments.order_by("position", "id"))
        if not scenes or any(not scene.image for scene in scenes):
            raise RuntimeError("Ajoute une image à chaque scène avant de générer la vidéo.")
        if any(not scene.audio or not Path(scene.audio.path).is_file() for scene in scenes):
            raise RuntimeError("La narration de certaines scènes n’est pas prête. Relance la préparation.")

        ffmpeg = _ffmpeg_path()
        width, height = _dimensions(project.video_format)
        with tempfile.TemporaryDirectory(prefix=f"petit-studio-video-{project.pk}-") as temp:
            folder = Path(temp)
            clips = []
            for index, scene in enumerate(scenes, start=1):
                project.status_message = f"Rendu vidéo · scène {index}/{len(scenes)}"
                project.current_segment = index
                project.progress_percent = 5 + round(85 * (index - 1) / len(scenes))
                project.save(update_fields=["status_message", "current_segment", "progress_percent", "updated_at"])

                clip = folder / f"scene-{index:04}.mp4"
                duration = max(0.1, float(scene.duration_seconds))
                subtitle_file = None
                if project.subtitles_enabled:
                    subtitle_file = folder / f"scene-{index:04}.srt"
                    subtitle_file.write_text(srt_text([scene]), encoding="utf-8")
                    if not subtitle_file.read_text(encoding="utf-8").strip():
                        raise RuntimeError(f"Aucun sous-titre synchronisé n’est disponible pour la scène {index}.")

                if scene.motion == NarrationSegment.Motion.PARALLAX:
                    project.status_message = f"Rendu vidéo · scène {index}/{len(scenes)} · profondeur douce"
                    project.save(update_fields=["status_message", "updated_at"])
                    _render_parallax_clip(
                        ffmpeg, scene.image.path, scene.audio.path, clip, subtitle_file,
                        width, height, duration, folder,
                    )
                else:
                    filters = _movement_filters(scene.motion, width, height, duration)
                    if subtitle_file:
                        filters.append(_subtitle_filter(subtitle_file))
                    command = [
                        ffmpeg, "-y", "-v", "error", "-loop", "1", "-i", str(scene.image.path),
                        "-i", str(scene.audio.path), "-t", f"{duration:.3f}", "-vf", ",".join(filters),
                        "-r", "24", "-c:v", "libx264", "-preset", "ultrafast", "-crf", "24",
                        "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k", "-ar", "48000",
                        "-ac", "2", "-shortest", str(clip),
                    ]
                    _run_ffmpeg(command, folder, f"rendu de la scène {index}")
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
                folder, "assemblage des scènes",
            )
            if not output.is_file() or output.stat().st_size < 1000:
                raise RuntimeError("FFmpeg n’a pas produit de fichier MP4 valide.")
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
    except Exception as exc:
        NarrationProject.objects.filter(pk=project_id).update(
            status=NarrationProject.Status.ERROR,
            status_message="Le rendu de la vidéo a échoué.",
            error_message=str(exc)[:2000],
            finished_at=timezone.now(),
        )
    finally:
        close_old_connections()
