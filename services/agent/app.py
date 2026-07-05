import base64
import json
import logging
import os
import posixpath
import re
import time
from langchain_core.rate_limiters import InMemoryRateLimiter
from contextvars import ContextVar
from typing import List, Optional
from uuid import uuid4

from dotenv import load_dotenv
load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logging.getLogger("langchain").setLevel(logging.DEBUG)
logging.getLogger("langchain_core").setLevel(logging.DEBUG)

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from langchain.chat_models import init_chat_model
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import tool
from pydantic import BaseModel

from s3 import download_file_bytes, upload_file_bytes
from mcp_client import call_mcp_tool

YOLO_SERVICE_URL = os.environ.get("YOLO_SERVICE_URL", "http://localhost:8080")
MODEL = os.environ.get("MODEL")
AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")
MODEL_PROVIDER = os.environ.get("MODEL_PROVIDER", "bedrock_converse")

ALLOWED_MODELS = {
    "anthropic.claude-3-haiku-20240307-v1:0",
    "amazon.nova-micro-v1:0",
    "amazon.nova-lite-v1:0",
    "openai.gpt-oss-20b-1:0",
    "meta.llama3-1-8b-instruct-v1:0",
    "mistral.mistral-7b-instruct-v0:2"
}

if MODEL not in ALLOWED_MODELS:
    allowed_list = "\n  ".join(sorted(ALLOWED_MODELS))
    raise SystemExit(
        f"\n[ERROR] MODEL='{MODEL}' is not allowed.\n"
        f"Set MODEL in your .env to one of the supported text-only models:\n  {allowed_list}\n"
    )

SYSTEM_PROMPT = (
    "You are an AI vision assistant. You help users analyze and edit images via tools. "
    "To analyze an image, you must run `detect_objects`. "
    "If the user asks what is in the image, you must run `detect_objects` first to get a list of detected objects, then you You MUST read the tool output data from `detect_objects` and write a detailed, natural paragraph summary breaking down exactly what items were found. "
    "If the user asks to see the annotated image, you must run `show_annotated_image` to get a public URL of the annotated image with bounding boxes. "
    #"CRITICAL OUTPUT RULES:"
    #"1. If the user did not ask to see the annotated image, do not include it in your response. but you can ask him if he wants to see it. "
    #"2. If the user asked to see the annotated image, do not print raw image URLs just let the image appear directly. "
    "If the user did not ask to see the annotated image, do not run `show_annotated_image`. but you can ask him if he wants to see it if his previous message was what is in the image ?. "
    "If the user asked what is in the image ? and to show the annotated image in the same message, you must run `detect_objects` first, then after reading the tool output data from `detect_objects`you must run `show_annotated_image` to include the annotated image in your response. "
    "Never print the raw `box` coordinate arrays (or a coordinates table) in your response to the user - "
    "use them internally to identify which object is which, but describe objects in plain language instead (e.g. 'the person on the left', 'the car near the top'). "
    "Do not include raw XML tags like `<thinking>` or `</thinking>` in your text reply. "
    #"if the user said yes for the annotated image, you must include it in your response. not the link, just the image itself. "
    "\n"
    "IMAGE EDITING: You also have editing tools: `rotate`, `flip`, `blur`, `resize`, `crop`, `add_noise`. "
    "Each edits the image currently being worked on and returns the result automatically to the user - you do not need a separate 'show' tool after an edit. "
    "Edits can be chained in the same turn (e.g. rotate then blur); each edit builds on the previous edit's result. "
    "To edit the WHOLE image, call the tool without a `bbox` argument. "
    "To edit a SPECIFIC detected object (e.g. 'blur the second dog from the right', 'add noise to the detected car'), you must first call `detect_objects` to get a `detections` list, "
    "where each entry has `label`, `score`, and `box` (a `[x1, y1, x2, y2]` pixel rectangle). "
    "Reason over these box coordinates yourself to identify the requested object - e.g. for 'the Nth <label> from the right', filter detections to that label and sort by `box[0]` (the left edge, x1) descending, then pick the Nth one. "
    "For 'from the left' sort ascending; for 'from the top'/'from the bottom' sort by `box[1]` (y1) ascending/descending. "
    "Then call the matching editing tool, passing that detection's `box` as the `bbox` argument. "
    "Box coordinates and labels are plain numbers/text, not image pixel data, so it is fine for you to see and reason over them. "
    "Note: `rotate` only accepts angles that are multiples of 90 when `bbox` is given (whole-image rotation allows any angle). "
    "`crop` requires a `bbox` and returns just that cropped region as the final image, not composited back into the full picture. "
    "\n"
    "NEVER emit markdown image syntax like `![alt](url)` or any inline image link for any image (annotated or edited) - "
    "you have no real URL or image bytes to put there, so it will only ever render broken. "
    "Every image (annotated_image, edited_image, image_url) is delivered automatically out-of-band and displayed by the frontend "
    "separately from your text - just describe what was done in plain language and let the image appear on its own. "
)

class TokenUsage(BaseModel):
    input: int
    output: int
    total: int

class AgentChatResponse(BaseModel):
    response: str
    prediction_id: Optional[str] = None
    predicted_image_s3_key: Optional[str] = None
    annotated_image: Optional[str] = None
    edited_image: Optional[str] = None
    current_image_s3_key: Optional[str] = None
    image_url: Optional[str] = None
    agent_loop_time_s: float
    iterations: int
    tools_called: List[str]
    context_limit_exceeded: bool
    tokens_used: TokenUsage  # Added token usage


_current_image_b64: ContextVar[Optional[str]] = ContextVar("current_image_b64", default=None)
_current_image_s3_key: ContextVar[Optional[str]] = ContextVar("current_image_s3_key", default=None)
_current_prediction_id: ContextVar[Optional[str]] = ContextVar("current_prediction_id", default=None)
_current_predicted_image_s3_key: ContextVar[Optional[str]] = ContextVar("current_predicted_image_s3_key", default=None)

IMAGE_EDIT_TOOL_NAMES = {"rotate", "flip", "blur", "resize", "crop", "add_noise"}


def _parse_bbox(box_str: str) -> List[float]:
    try:
        box = json.loads(box_str)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError(f"Malformed bbox string from YOLO: {box_str!r}") from exc
    if not isinstance(box, list) or len(box) != 4:
        raise ValueError(f"Expected a 4-element bbox list, got: {box_str!r}")
    return [float(v) for v in box]


def build_original_image_key(chat_id: str, prediction_id: str, image_ext: str = ".jpg") -> str:
    sanitized_ext = image_ext.lower() if image_ext.startswith(".") else f".{image_ext.lower()}"
    return posixpath.join(chat_id, prediction_id, "original", f"image{sanitized_ext}")


def upload_base64_image(image_b64: str, object_key: str) -> str:
    image_bytes = base64.b64decode(image_b64)
    uploaded = upload_file_bytes(image_bytes, object_key, content_type="image/jpeg")
    if not uploaded:
        raise RuntimeError(f"Failed to upload image to S3 key {object_key}")
    return object_key


def download_image_base64(object_key: str) -> Optional[str]:
    if not object_key:
        return None

    image_bytes = download_file_bytes(object_key)
    return base64.b64encode(image_bytes).decode("ascii")


_MARKDOWN_IMAGE_RE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_IMAGE_PLACEHOLDER_RE = re.compile(r"</?image\s*/?>", re.IGNORECASE)


def _strip_markdown_images(text: str) -> str:
    """Remove any markdown image syntax or literal <image> placeholder tokens the model
    hallucinates (it has no real image bytes or URL to reference, so ![alt](url) or a bare
    <image> tag always renders as either a broken image or dead text in the frontend)."""
    stripped = _MARKDOWN_IMAGE_RE.sub("", text)
    stripped = _IMAGE_PLACEHOLDER_RE.sub("", stripped)
    lines = [line.rstrip() for line in stripped.split("\n")]
    collapsed = re.sub(r"\n{3,}", "\n\n", "\n".join(lines))
    return collapsed.strip()


def _normalize_response_content(content) -> str:
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(str(block.get("text", "")))
            elif isinstance(block, str):
                parts.append(block)
        return "".join(parts) if parts else str(content)
    if isinstance(content, str):
        return content
    return str(content)


def _fetch_annotated_image(prediction_id: Optional[str]) -> Optional[str]:
    predicted_image_s3_key = _current_predicted_image_s3_key.get()
    if predicted_image_s3_key:
        try:
            annotated_image = download_image_base64(predicted_image_s3_key)
            if annotated_image:
                return annotated_image
        except Exception as exc:
            logging.warning("Failed to fetch annotated image from S3 key %s: %s", predicted_image_s3_key, exc)

    if not prediction_id:
        return None

    try:
        with httpx.Client(timeout=30.0) as client:
            response = client.get(f"{YOLO_SERVICE_URL}/prediction/{prediction_id}/image")
            response.raise_for_status()
        return base64.b64encode(response.content).decode("ascii")
    except Exception as exc:
        logging.warning("Failed to fetch annotated image for %s: %s", prediction_id, exc)
        return None

@tool
def show_annotated_image() -> str:
    """Retrieves the public URL of the annotated image containing YOLO bounding boxes.

    Use this tool ONLY when the user explicitly requests to see the visual image or photo.
    Requires a successful prior execution of detect_objects to provide a valid tracking UID.
    """
    prediction_uid = _current_prediction_id.get()

    if not prediction_uid:
        return json.dumps({
            "error": "No object detection has been performed yet in this session. Run detect_objects first."
        })

    image_url = f"{YOLO_SERVICE_URL}/prediction/{prediction_uid}/image"
    return json.dumps({"image_url": image_url})

@tool
def detect_objects() -> str:
    """Detect and identify objects in the image provided by the user using YOLO object detection.

    Returns a `detections` list with each object's `label`, `score`, and `box` ([x1, y1, x2, y2] pixel
    coordinates), which you can reason over to target a specific object (e.g. "the second dog from the right")
    for the image-editing tools.
    """
    image_s3_key = _current_image_s3_key.get()
    if not image_s3_key:
        return json.dumps({"error": "No image was provided by the user."})

    try:
        with httpx.Client(timeout=30.0) as client:
            response = client.post(f"{YOLO_SERVICE_URL}/predict", json={"image_s3_key": image_s3_key})
            response.raise_for_status()
            result = response.json()

            prediction_uid = result.get("prediction_uid")
            if prediction_uid:
                detail_response = client.get(f"{YOLO_SERVICE_URL}/prediction/{prediction_uid}")
                detail_response.raise_for_status()
                detection_objects = detail_response.json().get("detection_objects", [])
                result["detections"] = [
                    {
                        "id": obj.get("id"),
                        "label": obj.get("label"),
                        "score": obj.get("score"),
                        "box": _parse_bbox(obj.get("box")),
                    }
                    for obj in detection_objects
                ]
        return json.dumps(result)
    except httpx.HTTPStatusError as exc:
        detail = None
        try:
            detail = exc.response.json().get("detail")
        except Exception:
            detail = exc.response.text
        return json.dumps({
            "error": "YOLO service request failed.",
            "status_code": exc.response.status_code,
            "detail": detail,
        })
    except httpx.RequestError as exc:
        return json.dumps({
            "error": "YOLO service is unavailable.",
            "detail": str(exc),
        })


def _call_image_edit_tool(tool_name: str, arguments: dict) -> str:
    image_s3_key = _current_image_s3_key.get()
    if not image_s3_key:
        return json.dumps({"error": "No image available to edit. Upload an image first."})
    try:
        result = call_mcp_tool(tool_name, {"image_s3_key": image_s3_key, **arguments})
    except Exception as exc:
        return json.dumps({"error": "img-proc-mcp request failed.", "detail": str(exc)})
    return json.dumps(result)


@tool
def rotate(angle: float, bbox: Optional[List[float]] = None) -> str:
    """Rotate the current image by `angle` degrees counter-clockwise.
    Omit `bbox` to rotate the whole image (any angle). Pass a detection's `box` as `bbox` to rotate
    just that region (angle must then be a multiple of 90)."""
    return _call_image_edit_tool("rotate", {"angle": angle, "bbox": bbox})


@tool
def flip(direction: str = "horizontal", bbox: Optional[List[float]] = None) -> str:
    """Flip the current image. direction is 'horizontal' or 'vertical'.
    Omit `bbox` to flip the whole image, or pass a detection's `box` to flip just that region."""
    return _call_image_edit_tool("flip", {"direction": direction, "bbox": bbox})


@tool
def blur(radius: float = 2.0, bbox: Optional[List[float]] = None) -> str:
    """Apply Gaussian blur to the current image.
    Omit `bbox` to blur the whole image, or pass a detection's `box` to blur just that region."""
    return _call_image_edit_tool("blur", {"radius": radius, "bbox": bbox})


@tool
def resize(width: int, height: int, bbox: Optional[List[float]] = None) -> str:
    """Resize the current image to (width, height).
    Omit `bbox` to resize the whole image; pass a detection's `box` to stretch just that region
    to the new size in place."""
    return _call_image_edit_tool("resize", {"width": width, "height": height, "bbox": bbox})


@tool
def crop(bbox: List[float]) -> str:
    """Crop out a bbox region of the current image and return it as its own standalone image.
    `bbox` is required (e.g. a detection's `box`) - the result is the cropped region itself,
    not composited back into the full image."""
    return _call_image_edit_tool("crop", {"bbox": bbox})


@tool
def add_noise(amount: float = 0.05, bbox: Optional[List[float]] = None) -> str:
    """Add salt-and-pepper noise to the current image. `amount` is the fraction of pixels affected (0-1).
    Omit `bbox` to affect the whole image, or pass a detection's `box` to affect just that region."""
    return _call_image_edit_tool("add_noise", {"amount": amount, "bbox": bbox})


# Registry: map tool name -> tool function
TOOLS = {
    detect_objects.name: detect_objects,
    show_annotated_image.name: show_annotated_image,
    rotate.name: rotate,
    flip.name: flip,
    blur.name: blur,
    resize.name: resize,
    crop.name: crop,
    add_noise.name: add_noise,
}

# Initialize a rate limiter (30 Requests per minute baseline, max burst capacity of 2 requests)
rate_limiter = InMemoryRateLimiter(
    requests_per_second=0.5,      # Add credit for 1 request every 2 seconds
    check_every_n_seconds=0.1,    # Thread poll wake-up interval 
    max_bucket_size=2             # Maximum allowed burst window size
)

# Configuration update to instruct init_chat_model to use the Bedrock infrastructure layer
llm = init_chat_model(
    MODEL,
    model_provider=MODEL_PROVIDER,
    region_name=AWS_REGION, 
    temperature=0, 
    rate_limiter=rate_limiter
)
llm_with_tools = llm.bind_tools(list(TOOLS.values()))

# Capability check
try:
    profile = llm.profile or {}
except Exception:
    profile = {}

if profile:
    if not profile.get("tool_calling", False):
        raise SystemExit(
            f"\n[ERROR] MODEL='{MODEL}' does not support tool calling, "
            f"which this agent requires.\n"
        )
    MAX_INPUT_TOKENS = profile.get("max_input_tokens")
    logging.info(
        f"Model '{MODEL}' profile OK "
        f"(tool_calling=True, max_input_tokens={MAX_INPUT_TOKENS})"
    )
else:
    MAX_INPUT_TOKENS = None
    logging.warning(
        f"No capability profile available for MODEL='{MODEL}'. "
        f"Skipping capability check."
    )


def run_agent(history: list, max_iterations: int = 10) -> dict:
    """
    Simple ReAct loop with an infinite loop safety guard:
      1. Send messages to the LLM.
      2. If the LLM requests tool calls, execute them and append results.
      3. Repeat until the LLM returns a plain text response or max_iterations is reached.
    """
    messages = [SystemMessage(content=SYSTEM_PROMPT)] + history
    iterations = 0
    tools_called: List[str] = []
    prediction_id: Optional[str] = None
    predicted_image_s3_key: Optional[str] = None
    annotated_image: Optional[str] = None
    edited_image: Optional[str] = None
    last_edited_image_s3_key: Optional[str] = None
    image_url = None
    start_time = time.perf_counter()

# Accumulate tracking parameters over sequential agent steps
    total_input_tokens = 0
    total_output_tokens = 0
    context_limit_exceeded = False

    while iterations < max_iterations:
        iterations += 1
        logging.info(f"🤖 Agent iteration {iterations}/{max_iterations}")

        response: AIMessage = llm_with_tools.invoke(messages)
        messages.append(response)
	
	# Extract usage data safely from the runtime response metadata
        if hasattr(response, "usage_metadata") and response.usage_metadata:
            meta = response.usage_metadata
            total_input_tokens += meta.get("input_tokens", 0)
            total_output_tokens += meta.get("output_tokens", 0)
            
            # Switch context limit flag if input tokens exceed the profile threshold
            if MAX_INPUT_TOKENS and meta.get("input_tokens", 0) >= MAX_INPUT_TOKENS:
                logging.warning("⚠️ Approaching model max_input_tokens framework limits!")
                context_limit_exceeded = True

        if not response.tool_calls:
            loop_time = round(time.perf_counter() - start_time, 4)
            if last_edited_image_s3_key:
                edited_image = download_image_base64(last_edited_image_s3_key) or edited_image
            return {
                "response": _strip_markdown_images(_normalize_response_content(response.content)),
                "prediction_id": prediction_id,
                "predicted_image_s3_key": predicted_image_s3_key,
                "annotated_image": annotated_image,
                "edited_image": edited_image,
                "current_image_s3_key": _current_image_s3_key.get(),
                "image_url": image_url,
                "agent_loop_time_s": loop_time,
                "iterations": iterations,
                "tools_called": tools_called,
                "context_limit_exceeded": context_limit_exceeded,
                "tokens_used": {
                    "input": total_input_tokens,
                    "output": total_output_tokens,
                    "total": total_input_tokens + total_output_tokens
                }
            }

        for tool_call in response.tool_calls:
            tool_name = tool_call.get("name")
            tool_fn = TOOLS[tool_name]
            tool_result = tool_fn.invoke(tool_call)

            tool_output = tool_result.content if hasattr(tool_result, "content") else str(tool_result)
            tool_message = ToolMessage(
                content=tool_output, 
                tool_call_id=tool_call.get("id", ""), 
                name=tool_name
            )

            messages.append(tool_message)
            if tool_name:
                tools_called.append(tool_name)

            if tool_name == "detect_objects":
                tool_data = json.loads(tool_result.content)
                current_id = tool_data.get("prediction_id") or tool_data.get("prediction_uid")
                if current_id:
                    prediction_id = current_id
                    _current_prediction_id.set(current_id)
                current_predicted_key = tool_data.get("predicted_image_s3_key")
                if current_predicted_key:
                    predicted_image_s3_key = current_predicted_key
                    _current_predicted_image_s3_key.set(current_predicted_key)
            
            if tool_name == "show_annotated_image":
                tool_data = json.loads(tool_result.content)
                image_url = tool_data.get("image_url") or image_url
                annotated_image = _fetch_annotated_image(prediction_id) or annotated_image

            if tool_name in IMAGE_EDIT_TOOL_NAMES:
                tool_data = json.loads(tool_result.content)
                output_key = tool_data.get("output_s3_key")
                if output_key:
                    _current_image_s3_key.set(output_key)
                    last_edited_image_s3_key = output_key
                    # Any previously computed YOLO annotation is now stale (boxes
                    # would be misaligned against the edited image) - force a fresh
                    # detect_objects call if annotations are requested again.
                    prediction_id = None
                    predicted_image_s3_key = None
                    _current_prediction_id.set(None)
                    _current_predicted_image_s3_key.set(None)

    loop_time = round(time.perf_counter() - start_time, 4)
    error_msg = f"⚠️ Agent stopped automatically: Reached safety limit of {max_iterations} iterations without resolving."
    logging.warning(error_msg)
    if last_edited_image_s3_key:
        edited_image = download_image_base64(last_edited_image_s3_key) or edited_image
    return {
        "response": error_msg,
        "prediction_id": prediction_id,
        "predicted_image_s3_key": predicted_image_s3_key,
        "annotated_image": annotated_image,
        "edited_image": edited_image,
        "current_image_s3_key": _current_image_s3_key.get(),
        "image_url": image_url,
        "agent_loop_time_s": loop_time,
        "iterations": iterations,
        "tools_called": tools_called,
        "context_limit_exceeded": True,
        "tokens_used": {
            "input": total_input_tokens,
            "output": total_output_tokens,
            "total": total_input_tokens + total_output_tokens
        }
    }


app = FastAPI(title="Vision Agent")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", 
                   "http://sawalha.dev.fursa.click:3000" ,
                   "http://sawalha.prod.fursa.click:3000"],
    allow_methods=["POST", "GET"],
    allow_headers=["Content-Type"],
)


class ChatMessage(BaseModel):
    role: str                           # "user" or "assistant"
    content: str
    image_base64: Optional[str] = None  # only on user messages that carry an image
    current_image_s3_key: Optional[str] = None  # echoed back on assistant messages to carry state across turns
    prediction_id: Optional[str] = None  # echoed back on assistant messages to carry state across turns
    predicted_image_s3_key: Optional[str] = None  # echoed back on assistant messages to carry state across turns


class ChatRequest(BaseModel):
    messages: list[ChatMessage]         # full conversation thread, oldest first


@app.post("/chat", response_model=AgentChatResponse)
def chat(request: ChatRequest):
    lc_messages = []
    latest_image = None
    chat_id = str(uuid4())

    for msg in request.messages:
        if msg.role == "user":
            if msg.image_base64:
                latest_image = msg.image_base64          # saved for detect_objects tool
                content = msg.content + "\n[An image was uploaded. Use existing tools to analyze it according to user instructions.]"
            else:
                content = msg.content
            lc_messages.append(HumanMessage(content=content))
        else:
            lc_messages.append(AIMessage(content=msg.content))

    # The frontend resends the full message history every turn, so a historical
    # message's image_base64 must not be mistaken for a fresh upload: only the
    # newest message (last in the list) uploading an image counts as "new".
    last_message = request.messages[-1] if request.messages else None
    new_image_uploaded_this_turn = bool(
        last_message and last_message.role == "user" and last_message.image_base64
    )

    image_s3_key = None
    prediction_id = None
    predicted_image_s3_key = None
    if new_image_uploaded_this_turn:
        try:
            pred_uid = str(uuid4())
            s3_key = build_original_image_key(chat_id, pred_uid)
            image_s3_key = upload_base64_image(latest_image, s3_key)
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"Failed to upload image to S3: {exc}") from exc
        # A new image has nothing to do with any prior detection - prediction_id/
        # predicted_image_s3_key stay None so a fresh detect_objects call is required.
    else:
        # No new upload this turn - carry forward the most recent image-state
        # snapshot from history (current_image_s3_key/prediction_id/predicted_image_s3_key
        # were all echoed together on the same assistant message). Take all three from
        # the SAME message rather than independently merging fields from different
        # messages - an edit clears prediction_id/predicted_image_s3_key on its message,
        # and independently falling back to an older message's prediction_id there would
        # resurrect a prediction computed against a since-replaced image.
        for msg in reversed(request.messages):
            if msg.current_image_s3_key:
                image_s3_key = msg.current_image_s3_key
                prediction_id = msg.prediction_id
                predicted_image_s3_key = msg.predicted_image_s3_key
                break

    token_img = _current_image_b64.set(latest_image)
    token_img_s3 = _current_image_s3_key.set(image_s3_key)
    token_pred = _current_prediction_id.set(prediction_id)
    token_predicted_key = _current_predicted_image_s3_key.set(predicted_image_s3_key)
    try:
        agent_payload = run_agent(lc_messages)
        return agent_payload
    finally:
        _current_image_b64.reset(token_img)
        _current_image_s3_key.reset(token_img_s3)
        _current_prediction_id.reset(token_pred)
        _current_predicted_image_s3_key.reset(token_predicted_key)


@app.get("/health")
def health():
    return {"status": "ok"}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
