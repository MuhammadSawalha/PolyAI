import os

os.environ.setdefault("AWS_REGION", "us-east-1")
os.environ.setdefault("AWS_S3_BUCKET", "fake-bucket")
os.environ.setdefault("AWS_ACCESS_KEY_ID", "fake")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "fake")

import io
import pytest
from PIL import Image

import app as app_module

IMAGE_WIDTH = 40
IMAGE_HEIGHT = 20


def _make_test_image() -> Image.Image:
    # Blue background with a red left half, so bbox-scoped edits are
    # visually verifiable (pixels outside bbox must stay pure blue/red).
    img = Image.new("RGB", (IMAGE_WIDTH, IMAGE_HEIGHT), color=(0, 0, 255))
    left_half = Image.new("RGB", (IMAGE_WIDTH // 2, IMAGE_HEIGHT), color=(255, 0, 0))
    img.paste(left_half, (0, 0))
    return img


@pytest.fixture
def test_image() -> Image.Image:
    return _make_test_image()


@pytest.fixture
def test_image_bytes(test_image) -> bytes:
    buf = io.BytesIO()
    test_image.save(buf, format="PNG")
    return buf.getvalue()


@pytest.fixture
def uploaded_store():
    return {}


@pytest.fixture
def mock_s3(monkeypatch, test_image_bytes, uploaded_store):
    monkeypatch.setattr(app_module, "download_file_bytes", lambda key: test_image_bytes)

    def fake_upload(file_bytes, s3_key, content_type="image/jpeg"):
        uploaded_store[s3_key] = file_bytes
        return True

    monkeypatch.setattr(app_module, "upload_file_bytes", fake_upload)
    return uploaded_store


def uploaded_image(uploaded_store: dict, output_s3_key: str) -> Image.Image:
    return Image.open(io.BytesIO(uploaded_store[output_s3_key]))
