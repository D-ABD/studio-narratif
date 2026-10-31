"""Local FFmpeg rendering for narrated still-image scenes."""
import shutil
import subprocess
import tempfile
from pathlib import Path

from django.core.files import File
from django.db import close_old_connections
from django.utils import timezone

from .models import NarrationProject
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
                filters = _movement_filters(scene.motion, width, height, duration)
                if project.subtitles_enabled:
                    subtitle_file = folder / f"scene-{index:04}.srt"
                    subtitle_file.write_text(srt_text([scene]), encoding="utf-8")
                    if not subtitle_file.read_text(encoding="utf-8").strip():
                        raise RuntimeError(f"Aucun sous-titre synchronisé n’est disponible pour la scène {index}.")
                    filters.append(
                        "subtitles=filename=" + subtitle_file.name
                        + ":force_style='FontName=Arial,FontSize=24,PrimaryColour=&H00FFFFFF,"
                        + "BackColour=&H99000000,BorderStyle=3,Outline=0,Shadow=0,Alignment=2,MarginV=55'"
                    )
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
