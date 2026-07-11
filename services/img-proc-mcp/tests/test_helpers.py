import pytest

from app import build_edited_image_key, _clamp_bbox


def test_build_edited_image_key_normal():
    key = build_edited_image_key("chat123/pred456/original/image.jpg", "blur")
    parts = key.split("/")
    assert parts[0] == "chat123"
    assert parts[1] == "pred456"
    assert parts[2] == "edited"
    assert parts[3].startswith("blur_")
    assert parts[3].endswith(".png")


def test_build_edited_image_key_shallow_key():
    key = build_edited_image_key("justonepart", "rotate")
    parts = key.split("/")
    assert parts[0] == "justonepart"
    assert parts[2] == "edited"
    assert parts[3].startswith("rotate_")


def test_build_edited_image_key_empty_key_falls_back():
    key = build_edited_image_key("", "flip")
    parts = key.split("/")
    assert parts[0] == "chat"
    assert parts[2] == "edited"


def test_build_edited_image_key_unique_across_calls():
    key1 = build_edited_image_key("chat/pred/original/image.jpg", "blur")
    key2 = build_edited_image_key("chat/pred/original/image.jpg", "blur")
    assert key1 != key2


def test_clamp_bbox_within_bounds():
    assert _clamp_bbox([10, 10, 20, 20], 100, 100) == (10, 10, 20, 20)


def test_clamp_bbox_rounds_floats():
    assert _clamp_bbox([10.4, 10.6, 20.2, 20.8], 100, 100) == (10, 11, 20, 21)


def test_clamp_bbox_clamps_out_of_bounds():
    assert _clamp_bbox([-5, -5, 200, 200], 100, 100) == (0, 0, 100, 100)


def test_clamp_bbox_degenerate_x_raises():
    with pytest.raises(ValueError):
        _clamp_bbox([50, 10, 50, 60], 100, 100)


def test_clamp_bbox_degenerate_y_raises():
    with pytest.raises(ValueError):
        _clamp_bbox([10, 50, 60, 50], 100, 100)
