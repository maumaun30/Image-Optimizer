from pathlib import Path

from PIL import Image

from app.models import OutputFormat

_PIL_FORMAT = {
    OutputFormat.WEBP: "WEBP",
    OutputFormat.AVIF: "AVIF",
}

_EXTENSION = {
    OutputFormat.WEBP: ".webp",
    OutputFormat.AVIF: ".avif",
}


def process_image(
    input_path: str,
    output_dir: Path,
    output_format: OutputFormat,
    resize_width: int | None = None,
    quality: int = 85,
) -> tuple[str, int]:
    """Resize and convert an image. Returns (output_path, size_in_bytes)."""

    with Image.open(input_path) as img:
        original_format = img.format  # e.g. "JPEG", "PNG" — may be None after transforms

        # Resize preserving aspect ratio
        if resize_width and img.width != resize_width:
            ratio = resize_width / img.width
            new_height = max(1, int(img.height * ratio))
            img = img.resize((resize_width, new_height), Image.LANCZOS)

        if output_format == OutputFormat.ORIGINAL:
            pil_format = original_format or "JPEG"
            ext = Path(input_path).suffix.lower() or ".jpg"
        else:
            pil_format = _PIL_FORMAT[output_format]
            ext = _EXTENSION[output_format]

        # Flatten alpha for formats that don't support it
        if pil_format in ("JPEG",) and img.mode in ("RGBA", "LA", "P"):
            bg = Image.new("RGB", img.size, (255, 255, 255))
            if img.mode == "P":
                img = img.convert("RGBA")
            if img.mode in ("RGBA", "LA"):
                bg.paste(img, mask=img.split()[-1])
            img = bg
        elif pil_format in ("WEBP", "AVIF") and img.mode not in ("RGB", "RGBA", "L", "LA"):
            img = img.convert("RGBA")

        output_path = str(output_dir / f"{Path(input_path).stem}{ext}")

        save_kwargs: dict = {"quality": quality}
        if pil_format == "WEBP":
            save_kwargs["method"] = 6
        elif pil_format == "JPEG":
            save_kwargs["optimize"] = True
            save_kwargs["progressive"] = True
        elif pil_format == "PNG":
            save_kwargs.pop("quality")
            save_kwargs["optimize"] = True
        elif pil_format == "AVIF":
            pass  # quality already set

        img.save(output_path, format=pil_format, **save_kwargs)

    return output_path, Path(output_path).stat().st_size
