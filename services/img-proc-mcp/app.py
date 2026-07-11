import io
import logging
import posixpath
from typing import Callable, List, Optional, Tuple
from uuid import uuid4

import numpy as np
from dotenv import load_dotenv
load_dotenv()

from fastmcp import FastMCP
from PIL import Image, ImageFilter

from s3 import download_file_bytes, upload_file_bytes

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)

mcp = FastMCP("img-proc")


def _download_image(image_s3_key: str) -> Image.Image:
    image_bytes = download_file_bytes(image_s3_key)
    return Image.open(io.BytesIO(image_bytes)).convert("RGB")


def _upload_image(img: Image.Image, output_s3_key: str) -> str:
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    uploaded = upload_file_bytes(buf.getvalue(), output_s3_key, content_type="image/png")
    if not uploaded:
        raise RuntimeError(f"Failed to upload image to S3 key {output_s3_key}")
    return output_s3_key


def build_edited_image_key(image_s3_key: str, tool_name: str) -> str:
    parts = [p for p in image_s3_key.split("/") if p]
    chat_id = parts[0] if len(parts) > 0 else "chat"
    prediction_id = parts[1] if len(parts) > 1 else uuid4().hex
    filename = f"{tool_name}_{uuid4().hex}.png"
    return posixpath.join(chat_id, prediction_id, "edited", filename)


def _clamp_bbox(bbox: List[float], width: int, height: int) -> Tuple[int, int, int, int]:
    x1, y1, x2, y2 = bbox
    x1 = max(0, min(round(x1), width))
    y1 = max(0, min(round(y1), height))
    x2 = max(0, min(round(x2), width))
    y2 = max(0, min(round(y2), height))
    if x2 <= x1 or y2 <= y1:
        raise ValueError(f"Invalid bbox {bbox} for image of size {width}x{height}")
    return x1, y1, x2, y2


def _apply_in_place(
    img: Image.Image,
    bbox: Optional[List[float]],
    transform: Callable[[Image.Image], Image.Image],
) -> Image.Image:
    if bbox is None:
        return transform(img)
    x1, y1, x2, y2 = _clamp_bbox(bbox, img.width, img.height)
    region = img.crop((x1, y1, x2, y2))
    transformed = transform(region)
    if transformed.size != region.size:
        transformed = transformed.resize(region.size)
    result = img.copy()
    result.paste(transformed, (x1, y1))
    return result


def _finish(img: Image.Image, image_s3_key: str, tool_name: str) -> dict:
    output_s3_key = build_edited_image_key(image_s3_key, tool_name)
    _upload_image(img, output_s3_key)
    return {"output_s3_key": output_s3_key, "width": img.width, "height": img.height}


_ROTATE_TRANSPOSE = {
    90: Image.Transpose.ROTATE_90,
    180: Image.Transpose.ROTATE_180,
    270: Image.Transpose.ROTATE_270,
}


@mcp.tool()
def rotate(image_s3_key: str, angle: float, bbox: Optional[List[float]] = None) -> dict:
    """Rotate the whole image (any angle) or a bbox region (angle must be a multiple of 90) by `angle` degrees counter-clockwise. Returns {output_s3_key, width, height}."""
    img = _download_image(image_s3_key)
    if bbox is None:
        rotated = img.rotate(angle, expand=True)
    else:
        if angle % 90 != 0:
            raise ValueError("angle must be a multiple of 90 when bbox is given")
        # transpose() (not rotate(expand=False)) avoids black-filled corners on
        # non-square regions, since it properly swaps width/height for 90/270;
        # _apply_in_place then resizes the transposed region back to the
        # original footprint so the paste-back offset stays valid.
        transpose_mode = _ROTATE_TRANSPOSE.get(int(angle) % 360)
        transform = (lambda region: region.transpose(transpose_mode)) if transpose_mode else (lambda region: region)
        rotated = _apply_in_place(img, bbox, transform)
    return _finish(rotated, image_s3_key, "rotate")


@mcp.tool()
def flip(image_s3_key: str, direction: str = "horizontal", bbox: Optional[List[float]] = None) -> dict:
    """Flip the whole image or a bbox region. direction is 'horizontal' or 'vertical'. Returns {output_s3_key, width, height}."""
    if direction not in ("horizontal", "vertical"):
        raise ValueError("direction must be 'horizontal' or 'vertical'")
    transpose_mode = (
        Image.Transpose.FLIP_LEFT_RIGHT if direction == "horizontal" else Image.Transpose.FLIP_TOP_BOTTOM
    )
    img = _download_image(image_s3_key)
    flipped = _apply_in_place(img, bbox, lambda region: region.transpose(transpose_mode))
    return _finish(flipped, image_s3_key, "flip")


@mcp.tool()
def blur(image_s3_key: str, radius: float = 2.0, bbox: Optional[List[float]] = None) -> dict:
    """Apply Gaussian blur to the whole image or a bbox region. Returns {output_s3_key, width, height}."""
    img = _download_image(image_s3_key)
    blurred = _apply_in_place(img, bbox, lambda region: region.filter(ImageFilter.GaussianBlur(radius)))
    return _finish(blurred, image_s3_key, "blur")


@mcp.tool()
def resize(image_s3_key: str, width: int, height: int, bbox: Optional[List[float]] = None) -> dict:
    """Resize the whole image to (width, height); if bbox is given, stretch just that region in place to the new size. Returns {output_s3_key, width, height}."""
    if width <= 0 or height <= 0:
        raise ValueError("width and height must be positive")
    img = _download_image(image_s3_key)
    if bbox is None:
        resized = img.resize((width, height))
    else:
        x1, y1, x2, y2 = _clamp_bbox(bbox, img.width, img.height)
        region = img.crop((x1, y1, x2, y2)).resize((width, height))
        resized = img.copy()
        resized.paste(region, (x1, y1))
    return _finish(resized, image_s3_key, "resize")


@mcp.tool()
def crop(image_s3_key: str, bbox: List[float]) -> dict:
    """Crop out a bbox region and return it as its own standalone image (bbox is required). Returns {output_s3_key, width, height}."""
    img = _download_image(image_s3_key)
    x1, y1, x2, y2 = _clamp_bbox(bbox, img.width, img.height)
    cropped = img.crop((x1, y1, x2, y2))
    return _finish(cropped, image_s3_key, "crop")


@mcp.tool()
def add_noise(image_s3_key: str, amount: float = 0.05, bbox: Optional[List[float]] = None) -> dict:
    """Add salt-and-pepper noise to the whole image or a bbox region. amount is the fraction of pixels affected (0-1). Returns {output_s3_key, width, height}."""
    if not 0 <= amount <= 1:
        raise ValueError("amount must be between 0 and 1")

    def _salt_and_pepper(region: Image.Image) -> Image.Image:
        arr = np.array(region)
        mask = np.random.rand(*arr.shape[:2])
        arr[mask < amount / 2] = 0
        arr[mask > 1 - amount / 2] = 255
        return Image.fromarray(arr)

    img = _download_image(image_s3_key)
    noisy = _apply_in_place(img, bbox, _salt_and_pepper)
    return _finish(noisy, image_s3_key, "add_noise")


if __name__ == "__main__":  # pragma: no cover
    mcp.run(transport="http", host="0.0.0.0", port=9000)
