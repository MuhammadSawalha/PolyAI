import pytest

import app
from app import rotate, flip, blur, resize, crop, add_noise
from conftest import uploaded_image, IMAGE_WIDTH, IMAGE_HEIGHT

IMAGE_S3_KEY = "chat123/pred456/original/image.jpg"

# Straddles the red(left)/blue(right) boundary at x=20, away from image edges.
STRADDLING_BBOX = [15, 5, 25, 15]


def test_rotate_whole_image_expands_canvas(mock_s3):
    result = rotate(IMAGE_S3_KEY, angle=90)
    assert result["width"] == IMAGE_HEIGHT
    assert result["height"] == IMAGE_WIDTH


def test_rotate_bbox_leaves_outside_pixels_untouched(mock_s3):
    result = rotate(IMAGE_S3_KEY, angle=90, bbox=STRADDLING_BBOX)
    out = uploaded_image(mock_s3, result["output_s3_key"])
    assert out.size == (IMAGE_WIDTH, IMAGE_HEIGHT)
    assert out.getpixel((5, 5)) == (255, 0, 0)
    assert out.getpixel((35, 5)) == (0, 0, 255)


def test_rotate_bbox_rejects_non_90_multiple(mock_s3):
    with pytest.raises(ValueError):
        rotate(IMAGE_S3_KEY, angle=45, bbox=STRADDLING_BBOX)


def test_rotate_bbox_non_square_region_keeps_footprint(mock_s3):
    # 90/270 rotation swaps width/height of a non-square region; the result
    # must be resized back to fit the original footprint on paste-back.
    result = rotate(IMAGE_S3_KEY, angle=90, bbox=[10, 5, 30, 15])
    out = uploaded_image(mock_s3, result["output_s3_key"])
    assert out.size == (IMAGE_WIDTH, IMAGE_HEIGHT)


def test_flip_whole_image_mirrors_left_right(mock_s3):
    result = flip(IMAGE_S3_KEY, direction="horizontal")
    out = uploaded_image(mock_s3, result["output_s3_key"])
    # Left half was red, right half was blue -> after horizontal flip, reversed.
    assert out.getpixel((5, 5)) == (0, 0, 255)
    assert out.getpixel((35, 5)) == (255, 0, 0)


def test_flip_bbox_leaves_outside_pixels_untouched(mock_s3):
    result = flip(IMAGE_S3_KEY, direction="vertical", bbox=STRADDLING_BBOX)
    out = uploaded_image(mock_s3, result["output_s3_key"])
    assert out.getpixel((5, 5)) == (255, 0, 0)
    assert out.getpixel((35, 5)) == (0, 0, 255)


def test_flip_rejects_invalid_direction(mock_s3):
    with pytest.raises(ValueError):
        flip(IMAGE_S3_KEY, direction="diagonal")


def test_blur_whole_image_blends_boundary(mock_s3):
    result = blur(IMAGE_S3_KEY, radius=3.0)
    out = uploaded_image(mock_s3, result["output_s3_key"])
    # Near the red/blue boundary, blur should blend colors (neither pure red nor pure blue).
    boundary_pixel = out.getpixel((20, 10))
    assert boundary_pixel != (255, 0, 0)
    assert boundary_pixel != (0, 0, 255)


def test_blur_bbox_leaves_outside_pixels_untouched(mock_s3):
    result = blur(IMAGE_S3_KEY, radius=3.0, bbox=STRADDLING_BBOX)
    out = uploaded_image(mock_s3, result["output_s3_key"])
    assert out.getpixel((5, 5)) == (255, 0, 0)
    assert out.getpixel((35, 5)) == (0, 0, 255)
    # Inside the bbox, near the boundary, the color should now be blended.
    blended = out.getpixel((20, 10))
    assert blended != (255, 0, 0)
    assert blended != (0, 0, 255)


def test_resize_whole_image(mock_s3):
    result = resize(IMAGE_S3_KEY, width=10, height=5)
    assert result["width"] == 10
    assert result["height"] == 5


def test_resize_bbox_changes_only_region_footprint(mock_s3):
    result = resize(IMAGE_S3_KEY, width=6, height=6, bbox=[25, 5, 35, 15])
    out = uploaded_image(mock_s3, result["output_s3_key"])
    # Canvas size is unchanged; only the bbox region's content is stretched.
    assert out.size == (IMAGE_WIDTH, IMAGE_HEIGHT)
    assert out.getpixel((5, 5)) == (255, 0, 0)


def test_resize_rejects_non_positive_dimensions(mock_s3):
    with pytest.raises(ValueError):
        resize(IMAGE_S3_KEY, width=0, height=10)


def test_crop_returns_only_the_region(mock_s3):
    result = crop(IMAGE_S3_KEY, bbox=[0, 0, 20, 20])
    assert result["width"] == 20
    assert result["height"] == 20
    out = uploaded_image(mock_s3, result["output_s3_key"])
    assert out.size == (20, 20)
    assert out.getpixel((5, 5)) == (255, 0, 0)


def test_crop_requires_bbox():
    with pytest.raises(TypeError):
        crop(IMAGE_S3_KEY)


def test_crop_rejects_degenerate_bbox(mock_s3):
    with pytest.raises(ValueError):
        crop(IMAGE_S3_KEY, bbox=[10, 10, 10, 20])


def test_add_noise_whole_image_changes_some_pixels(mock_s3):
    result = add_noise(IMAGE_S3_KEY, amount=0.5)
    out = uploaded_image(mock_s3, result["output_s3_key"])
    pixels = list(out.getdata())
    assert any(p not in ((255, 0, 0), (0, 0, 255)) for p in pixels)


def test_add_noise_bbox_leaves_outside_pixels_untouched(mock_s3):
    result = add_noise(IMAGE_S3_KEY, amount=0.9, bbox=[25, 5, 35, 15])
    out = uploaded_image(mock_s3, result["output_s3_key"])
    assert out.getpixel((5, 5)) == (255, 0, 0)
    assert out.getpixel((0, 0)) == (255, 0, 0)


def test_add_noise_rejects_out_of_range_amount(mock_s3):
    with pytest.raises(ValueError):
        add_noise(IMAGE_S3_KEY, amount=1.5)


def test_upload_failure_raises(monkeypatch, test_image_bytes):
    monkeypatch.setattr(app, "download_file_bytes", lambda key: test_image_bytes)
    monkeypatch.setattr(app, "upload_file_bytes", lambda *a, **k: False)
    with pytest.raises(RuntimeError):
        blur(IMAGE_S3_KEY, radius=2.0)


def test_download_failure_propagates(monkeypatch):
    def fail_download(key):
        raise RuntimeError("S3 download failed")

    monkeypatch.setattr(app, "download_file_bytes", fail_download)
    with pytest.raises(RuntimeError):
        blur(IMAGE_S3_KEY, radius=2.0)
