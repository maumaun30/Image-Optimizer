import shutil
import subprocess
from pathlib import Path

import pikepdf

from app.models import CompressionLevel

# Ghostscript -dPDFSETTINGS presets (image downsampling DPI baked into each)
_GS_PDFSETTINGS = {
    CompressionLevel.SCREEN: "/screen",
    CompressionLevel.EBOOK: "/ebook",
    CompressionLevel.PRINTER: "/printer",
}


def _ghostscript_bin() -> str | None:
    """Return the Ghostscript executable name if it's on PATH, else None."""
    for name in ("gs", "gswin64c", "gswin32c"):
        if shutil.which(name):
            return name
    return None


def _compress_with_ghostscript(
    gs_bin: str, input_path: str, output_path: str, level: CompressionLevel
) -> bool:
    """Run Ghostscript to downsample images and recompress. Returns True on success."""
    cmd = [
        gs_bin,
        "-sDEVICE=pdfwrite",
        "-dCompatibilityLevel=1.7",
        f"-dPDFSETTINGS={_GS_PDFSETTINGS[level]}",
        "-dNOPAUSE",
        "-dQUIET",
        "-dBATCH",
        "-dAutoRotatePages=/None",
        f"-sOutputFile={output_path}",
        input_path,
    ]
    result = subprocess.run(cmd, capture_output=True, timeout=600)
    return result.returncode == 0 and Path(output_path).exists()


def _compress_with_pikepdf(input_path: str, output_path: str) -> None:
    """Lossless structural compression: object streams + stream recompression.
    Does not downsample embedded images, so gains are modest but always safe."""
    with pikepdf.open(input_path) as pdf:
        pdf.save(
            output_path,
            compress_streams=True,
            recompress_flate=True,
            object_stream_mode=pikepdf.ObjectStreamMode.generate,
            linearize=True,
        )


def process_pdf(
    input_path: str,
    output_dir: Path,
    compression_level: CompressionLevel,
) -> tuple[str, int]:
    """Compress a PDF. Returns (output_path, size_in_bytes).

    Uses Ghostscript when available (best results — downsamples images per preset),
    otherwise falls back to pikepdf's lossless structural compression. The `lossless`
    level always uses pikepdf. If compression makes the file larger (already-optimized
    PDFs), the smaller of the two is kept.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = str(output_dir / f"{Path(input_path).stem}.pdf")

    gs_bin = _ghostscript_bin()
    used_gs = False
    if compression_level != CompressionLevel.LOSSLESS and gs_bin:
        used_gs = _compress_with_ghostscript(gs_bin, input_path, output_path, compression_level)

    if not used_gs:
        _compress_with_pikepdf(input_path, output_path)

    # Never hand back a file larger than the original.
    original_size = Path(input_path).stat().st_size
    if Path(output_path).stat().st_size > original_size:
        shutil.copyfile(input_path, output_path)

    return output_path, Path(output_path).stat().st_size
