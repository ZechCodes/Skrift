"""On-demand image variant generation using Pillow."""

from __future__ import annotations

import io

from PIL import Image

IMAGE_SIZES: dict[str, tuple[int, int | None]] = {
    "icon": (64, 64),
    "thumb": (200, 200),
    "small": (400, None),
    "medium": (800, None),
    "cover": (1200, None),
}

_FORMAT_TO_CONTENT_TYPE = {
    "JPEG": "image/jpeg",
    "PNG": "image/png",
    "WEBP": "image/webp",
    "GIF": "image/gif",
}


def detect_image_content_type(data: bytes) -> str | None:
    """Detect image content type from magic bytes.

    Returns ``None`` if the data does not match a known image signature.
    """
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    return None


def resize_image(
    data: bytes,
    max_width: int,
    max_height: int | None,
) -> tuple[bytes, str]:
    """Resize an image to fit within the given dimensions.

    Preserves aspect ratio and original format. Does not upscale if the
    original is already smaller than the target.

    Returns:
        A tuple of ``(resized_bytes, content_type)``.
    """
    img = Image.open(io.BytesIO(data))
    orig_format = img.format or "PNG"

    orig_w, orig_h = img.size

    if max_height is not None:
        # Fit within box — don't upscale
        if orig_w <= max_width and orig_h <= max_height:
            content_type = _FORMAT_TO_CONTENT_TYPE.get(orig_format, "image/png")
            return data, content_type

        img.thumbnail((max_width, max_height), Image.LANCZOS)
    else:
        # Width-constrained only — don't upscale
        if orig_w <= max_width:
            content_type = _FORMAT_TO_CONTENT_TYPE.get(orig_format, "image/png")
            return data, content_type

        ratio = max_width / orig_w
        new_h = int(orig_h * ratio)
        img = img.resize((max_width, new_h), Image.LANCZOS)

    buf = io.BytesIO()
    save_kwargs: dict = {}
    if orig_format == "JPEG":
        save_kwargs["quality"] = 85
        save_kwargs["optimize"] = True
    elif orig_format == "PNG":
        save_kwargs["optimize"] = True
    elif orig_format == "WEBP":
        save_kwargs["quality"] = 85

    img.save(buf, format=orig_format, **save_kwargs)
    content_type = _FORMAT_TO_CONTENT_TYPE.get(orig_format, "image/png")
    return buf.getvalue(), content_type


def variant_filename(key: str, size_name: str) -> str:
    """Derive the on-disk filename for a sized variant.

    E.g. ``variant_filename("abc123def.jpg", "thumb")`` → ``"abc123def.jpg.thumb"``
    """
    return f"{key}.{size_name}"
