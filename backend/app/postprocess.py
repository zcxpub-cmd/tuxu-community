"""Reversible, explicitly non-generative image processing for the demo."""
from pathlib import Path
import hashlib
import warnings
import os
import tempfile
from PIL import Image, ImageFilter, ImageOps

MAX_PIXELS = 16_000_000


def normalize_image_size(width: int, height: int, target_long_edge: int = 2048,
                         max_pixels: int = MAX_PIXELS) -> tuple[int, int]:
    """Enlarge to a minimum long edge; never shrink. Rounded ratio is preserved."""
    values = (width, height, target_long_edge, max_pixels)
    if any(type(v) is not int or v <= 0 for v in values):
        raise ValueError("尺寸和像素上限必须为正整数")
    if width * height > max_pixels:
        raise ValueError("原图超出像素上限")
    ratio = max(1.0, target_long_edge / max(width, height))
    result = max(1, round(width * ratio)), max(1, round(height * ratio))
    if result[0] * result[1] > max_pixels:
        raise ValueError("处理图超出像素上限")
    return result


def process_image(source_path: Path, output_path: Path, denoise: bool = True,
                  upscale: int = 2) -> dict:
    if type(upscale) is not int or not 1 <= upscale <= 4:
        raise ValueError("放大倍数须为1—4")
    if source_path.resolve() == output_path.resolve():
        raise ValueError("原图和处理图必须分开保存")
    with warnings.catch_warnings():
        warnings.simplefilter("error", Image.DecompressionBombWarning)
        with Image.open(source_path) as opened:
            # Reject oversized inputs before decoding their pixel data.
            w, h = opened.size
            normalize_image_size(w, h, max(w, h))
            img = ImageOps.exif_transpose(opened).convert("RGB")
    w, h = img.size
    target = max(2048, max(w, h) * upscale)
    size = normalize_image_size(w, h, target)
    if denoise:
        img = img.filter(ImageFilter.MedianFilter(size=3))
    if img.size != size:
        img = img.resize(size, Image.Resampling.LANCZOS)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=output_path.stem + '.', suffix='.tmp', dir=output_path.parent)
    os.close(descriptor)
    temporary = Path(name)
    try:
        img.save(temporary, format="PNG")
        result_hash = hashlib.sha256(temporary.read_bytes()).hexdigest()
        temporary.replace(output_path)
    finally:
        temporary.unlink(missing_ok=True)
    return {"width": size[0], "height": size[1], "original_width": w,
            "original_height": h, "denoise": bool(denoise), "upscale": upscale,
            "algorithm": "3×3中值轻度降噪 + Lanczos插值放大（非AI超分）" if denoise else "Lanczos插值放大（非AI超分）",
            "sha256": result_hash}
