# img-proc-mcp

An MCP-over-HTTP server exposing image-editing tools (rotate, flip, blur, resize, crop, add_noise) backed by S3. Consumed by `services/agent`, which calls these tools on behalf of the LLM to fulfill requests like "blur the second dog from the right" or "rotate the entire image 90 degrees".

## Prerequisites

- Python 3.10+
- An S3 bucket the service can read/write (`AWS_S3_BUCKET`)

## Setup

Install dependencies (from `services/img-proc-mcp/`):

```bash
pip install -r requirements.txt
```

Configure environment:

```bash
cp .env.example .env
# Edit .env and set AWS_S3_BUCKET
```

`.env` variables:

| Variable | Default | Description |
|---|---|---|
| `AWS_REGION` | `us-east-1` | AWS region for the S3 client |
| `AWS_S3_BUCKET` | - | S3 bucket used to read input images and write edited output images |

## Running

```bash
cd services/img-proc-mcp
python app.py
```

The MCP server starts at `http://localhost:9000/mcp` (streamable HTTP transport).

## Tools

Every tool takes an `image_s3_key` (the S3 key of the image to edit) and returns `{"output_s3_key": str, "width": int, "height": int}` pointing to the edited image, also stored in S3.

| Tool | Params | Notes |
|---|---|---|
| `rotate` | `angle: float`, `bbox: list[float] \| None` | Whole image: any angle. With `bbox`: `angle` must be a multiple of 90. |
| `flip` | `direction: "horizontal" \| "vertical"`, `bbox: list[float] \| None` | |
| `blur` | `radius: float = 2.0`, `bbox: list[float] \| None` | Gaussian blur. |
| `resize` | `width: int`, `height: int`, `bbox: list[float] \| None` | Whole image: resizes the canvas. With `bbox`: stretches just that region to the new size, pasted back at the same offset. |
| `crop` | `bbox: list[float]` (required) | Returns the cropped-out region itself — not composited back into the full image. |
| `add_noise` | `amount: float = 0.05`, `bbox: list[float] \| None` | Salt-and-pepper noise; `amount` is the fraction of affected pixels. |

`bbox` is `[x1, y1, x2, y2]` in pixel coordinates. When omitted (where supported), the operation applies to the whole image; when given, the operation is applied to just that region and composited back into the full-size image (except `crop`).

## Testing

```bash
pytest --cov=app --cov-report=term-missing
```
