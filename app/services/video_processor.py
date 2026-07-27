import json
import shutil
import subprocess
import tempfile
import time
from functools import lru_cache
from pathlib import Path
from typing import Callable

from app.models import VideoCodec, VideoPreset


class FfmpegMissingError(RuntimeError):
    """ffmpeg/ffprobe not installed. Unlike PDFs there is no pure-Python fallback."""


class VideoProcessingError(RuntimeError):
    pass


# CRF per codec — lower means better quality and a bigger file. The scales are not
# comparable across codecs, hence one table each.
_CRF = {
    VideoCodec.H264: {VideoPreset.LOW: 32, VideoPreset.BALANCED: 26, VideoPreset.HIGH: 22},
    VideoCodec.VP9: {VideoPreset.LOW: 40, VideoPreset.BALANCED: 34, VideoPreset.HIGH: 30},
    VideoCodec.AV1: {VideoPreset.LOW: 46, VideoPreset.BALANCED: 38, VideoPreset.HIGH: 32},
}

_AUDIO_BITRATE = {
    VideoPreset.LOW: "64k",
    VideoPreset.BALANCED: "96k",
    VideoPreset.HIGH: "128k",
}

_CONTAINER = {
    VideoCodec.H264: ".mp4",
    VideoCodec.VP9: ".webm",
    VideoCodec.AV1: ".mp4",
}

_ENCODER = {
    VideoCodec.H264: "libx264",
    VideoCodec.VP9: "libvpx-vp9",
    VideoCodec.AV1: "libsvtav1",
}


def ffmpeg_bin() -> str | None:
    return shutil.which("ffmpeg")


def ffprobe_bin() -> str | None:
    return shutil.which("ffprobe")


def _require_ffmpeg() -> tuple[str, str]:
    ffmpeg, ffprobe = ffmpeg_bin(), ffprobe_bin()
    if not ffmpeg or not ffprobe:
        raise FfmpegMissingError(
            "ffmpeg and ffprobe are required for video compression but were not found on PATH"
        )
    return ffmpeg, ffprobe


@lru_cache(maxsize=8)
def has_encoder(name: str) -> bool:
    """Check the local ffmpeg build actually ships the encoder (libsvtav1 often missing)."""
    ffmpeg = ffmpeg_bin()
    if not ffmpeg:
        return False
    result = subprocess.run(
        [ffmpeg, "-hide_banner", "-loglevel", "error", "-encoders"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    return name in result.stdout


def probe(input_path: str) -> dict:
    """Return {duration, width, height, has_audio}. Raises if there is no video stream."""
    _, ffprobe = _require_ffmpeg()
    result = subprocess.run(
        [
            ffprobe,
            "-v", "error",
            "-print_format", "json",
            "-show_format",
            "-show_streams",
            input_path,
        ],
        capture_output=True,
        text=True,
        timeout=120,
    )
    if result.returncode != 0:
        raise VideoProcessingError(f"Not a readable media file: {result.stderr.strip()[:300]}")

    data = json.loads(result.stdout or "{}")
    streams = data.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    if video is None:
        raise VideoProcessingError("File contains no video stream")

    duration = data.get("format", {}).get("duration") or video.get("duration")
    try:
        duration = float(duration)
    except (TypeError, ValueError):
        duration = None

    return {
        "duration": duration,
        "width": video.get("width"),
        "height": video.get("height"),
        "has_audio": any(s.get("codec_type") == "audio" for s in streams),
    }


def _build_command(
    ffmpeg: str,
    input_path: str,
    output_path: str,
    preset: VideoPreset,
    codec: VideoCodec,
    target_width: int | None,
    mute: bool,
    threads: int,
) -> list[str]:
    crf = str(_CRF[codec][preset])

    cmd = [ffmpeg, "-hide_banner", "-nostdin", "-y", "-loglevel", "error", "-i", input_path]

    if target_width:
        # Never upscale, and force even dimensions (-2) — required by yuv420p encoders.
        cmd += ["-vf", f"scale='min({target_width},iw)':-2"]

    if codec == VideoCodec.H264:
        cmd += ["-c:v", "libx264", "-crf", crf, "-preset", "veryfast", "-pix_fmt", "yuv420p"]
    elif codec == VideoCodec.VP9:
        cmd += [
            "-c:v", "libvpx-vp9",
            "-crf", crf,
            "-b:v", "0",
            "-row-mt", "1",
            "-deadline", "good",
            "-cpu-used", "4",
            "-pix_fmt", "yuv420p",
        ]
    else:  # AV1
        cmd += ["-c:v", "libsvtav1", "-crf", crf, "-preset", "8", "-pix_fmt", "yuv420p"]

    if mute:
        cmd += ["-an"]
    elif codec == VideoCodec.VP9:
        cmd += ["-c:a", "libopus", "-b:a", _AUDIO_BITRATE[preset]]
    else:
        cmd += ["-c:a", "aac", "-b:a", _AUDIO_BITRATE[preset]]

    if _CONTAINER[codec] == ".mp4":
        # Move the moov atom to the front so the file starts playing before it finishes downloading
        cmd += ["-movflags", "+faststart"]

    cmd += ["-threads", str(threads), "-progress", "pipe:1", "-nostats", output_path]
    return cmd


def _run_with_progress(
    cmd: list[str],
    duration: float | None,
    time_limit: int,
    on_progress: Callable[[int], None] | None,
) -> None:
    """Run ffmpeg, parsing `-progress pipe:1` output. Kills the process past time_limit.

    stderr goes to a temp file rather than a pipe: with only stdout being drained, a
    full stderr pipe buffer would deadlock the encode.
    """
    deadline = time.monotonic() + time_limit
    last_reported = 0
    saw_out_time_us = False

    with tempfile.TemporaryFile(mode="w+") as err_file:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=err_file,
            text=True,
            bufsize=1,
        )
        try:
            for line in proc.stdout:  # type: ignore[union-attr]
                if time.monotonic() > deadline:
                    proc.kill()
                    raise VideoProcessingError(
                        f"Encode exceeded the {time_limit}s time limit and was cancelled"
                    )

                key, _, value = line.strip().partition("=")
                # ffmpeg reports out_time_ms in *microseconds* (long-standing quirk),
                # so both keys are read the same way. Prefer out_time_us where the
                # build emits it, in case a build ever reports true milliseconds.
                if key == "out_time_us":
                    saw_out_time_us = True
                elif key == "out_time_ms":
                    if saw_out_time_us:
                        continue
                else:
                    continue
                if not duration:
                    continue
                try:
                    seconds = int(value) / 1_000_000
                except ValueError:
                    continue

                percent = min(99, int(seconds / duration * 100))
                if on_progress and percent >= last_reported + 5:
                    last_reported = percent
                    on_progress(percent)
        finally:
            if proc.stdout:
                proc.stdout.close()

        returncode = proc.wait(timeout=60)
        if returncode != 0:
            err_file.seek(0)
            raise VideoProcessingError(f"ffmpeg failed: {err_file.read().strip()[:500]}")


def process_video(
    input_path: str,
    output_dir: Path,
    preset: VideoPreset,
    codec: VideoCodec,
    target_width: int | None = None,
    mute: bool = False,
    threads: int = 2,
    time_limit: int = 7200,
    on_progress: Callable[[int], None] | None = None,
) -> tuple[str, int, int | None]:
    """Compress a video with ffmpeg. Returns (output_path, size_in_bytes, duration_seconds).

    Unlike the PDF pipeline there is no fallback encoder — ffmpeg is a hard dependency.
    If the result comes out bigger than the source (already-compressed input), the
    original is kept instead.
    """
    ffmpeg, _ = _require_ffmpeg()

    encoder = _ENCODER[codec]
    if not has_encoder(encoder):
        raise VideoProcessingError(
            f"This ffmpeg build has no {encoder} encoder — pick a different codec"
        )

    info = probe(input_path)
    duration = info["duration"]

    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = str(output_dir / f"{Path(input_path).stem}{_CONTAINER[codec]}")

    cmd = _build_command(
        ffmpeg, input_path, output_path, preset, codec, target_width, mute, threads
    )
    _run_with_progress(cmd, duration, time_limit, on_progress)

    if not Path(output_path).exists():
        raise VideoProcessingError("ffmpeg reported success but produced no output file")

    # Never hand back a file larger than the original. Fall back to the source bytes
    # under the source extension — the re-encoded container would be a lie otherwise.
    original_size = Path(input_path).stat().st_size
    if Path(output_path).stat().st_size > original_size:
        fallback = str(output_dir / f"{Path(input_path).stem}{Path(input_path).suffix}")
        Path(output_path).unlink(missing_ok=True)
        shutil.copyfile(input_path, fallback)
        output_path = fallback

    return (
        output_path,
        Path(output_path).stat().st_size,
        int(duration) if duration else None,
    )
