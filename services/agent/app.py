import asyncio
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
from fastapi import FastAPI, HTTPException, Response
from fastapi.middleware.cors import CORSMiddleware
from prometheus_client import Counter, Histogram, CONTENT_TYPE_LATEST, generate_latest
from langchain.chat_models import init_chat_model
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import tool
from langchain_mcp_adapters.client import MultiServerMCPClient
from pydantic import BaseModel

from s3 import download_file_bytes, upload_file_bytes

YOLO_SERVICE_URL = os.environ.get("YOLO_SERVICE_URL", "http://localhost:8080")
IMG_PROC_MCP_URL = os.environ.get("IMG_PROC_MCP_URL", "http://localhost:9000/mcp")
MODEL = os.environ.get("MODEL")
AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")
MODEL_PROVIDER = os.environ.get("MODEL_PROVIDER", "bedrock_converse")

ALLOWED_MODELS = {
    "anthropic.claude-3-haiku-20240307-v1:0",
    "us.anthropic.claude-haiku-4-5-20251001-v1:0",
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
    "(This summary requirement applies ONLY when the user asked what is in the image / to analyze it - it does NOT apply when `detect_objects` was called for a different reason, such as locating an object for an edit request; see IMAGE EDITING below for what to do in that case.) "
    "If the user asks to see the annotated image, you must run `show_annotated_image` to get a public URL of the annotated image with bounding boxes. "
    "If the user did not ask to see the annotated image, do not run `show_annotated_image`, but after answering a 'what is in the image' question you MUST ask him, every single time, whether he wants to see the annotated image with bounding boxes - never skip this offer. "
    "If the user asked what is in the image ? and to show the annotated image in the same message, you must run `detect_objects` first, then after reading the tool output data from `detect_objects`you must run `show_annotated_image`. "
    "If the user said yes after you asked him if he wants to see the annotated image, you must run only `show_annotated_image`. "
    "Never print the raw `box` coordinate arrays (or a coordinates table) in your response to the user - "
    "Never include an image URL in your response (str) or telling the user you can view the image by clicking on the following link. "
    "use them internally to identify which object is which, but describe objects in plain language instead (e.g. 'the person on the left', 'the car near the top'). "
    "Do not include raw XML tags like `<thinking>` or `</thinking>` in your text reply. "
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
    "IMPORTANT: calling `detect_objects` to locate an object for an edit is NOT the end of your turn - it only gives you coordinates, it never performs an edit by itself. "
    "You MUST immediately follow it, in this SAME turn, with a call to the matching editing tool (`rotate`/`flip`/`blur`/`resize`/`crop`/`add_noise`) using that detection's `box` as `bbox`. "
    "Do not stop after `detect_objects` and write a text response describing the edit as already done - an edit is only real once you have actually called the editing tool and it returned a new `output_s3_key` in this turn. "
    "Box coordinates and labels are plain numbers/text, not image pixel data, so it is fine for you to see and reason over them. "
    "Note: `rotate` only accepts angles that are multiples of 90 when `bbox` is given (whole-image rotation allows any angle). "
    "`crop` requires a `bbox` and returns just that cropped region as the final image, not composited back into the full picture. "
    "\n"
    "DETECTIONS NEVER CARRY OVER BETWEEN TURNS: you do not retain the raw `detections`/`box` data from any earlier turn in this conversation - "
    "only the plain-language description of what happened persists, never the coordinates themselves. "
    "Any earlier detection is also invalid once the image has been edited (positions and dimensions can shift). "
    "So whenever a NEW message asks you to identify an object by position (e.g. 'the Nth <label> from the left/right/top/bottom') for an edit, "
    "you must call `detect_objects` again in THIS turn to get fresh box coordinates - even if you or an earlier turn already detected objects earlier in this same conversation. "
    "Never assume you remember box coordinates from earlier turns, and never skip straight to an editing tool using positions you have not freshly detected in this turn. "
    "\n"
    "NEVER claim in your response that an image was rotated, flipped, blurred, resized, cropped, or had noise added unless you actually called that tool in THIS turn - "
    "do not describe a hypothetical or previously-done edit as if it just happened. If the user's current message asks for an edit you have not yet performed in this turn, call the tool before answering. "
    "\n"
    "NEVER emit markdown image syntax like `![alt](url)` or any inline image link for any image (annotated or edited) - "
    "you have no real URL or image bytes to put there, so it will only ever render broken. "
    "Every image (annotated_image, edited_image, image_url) is delivered automatically out-of-band and displayed by the frontend "
    "separately from your text - just describe what was done in plain language and let the image appear on its own. "
    "\n"
    "RESETTING: this conversation keeps track of the ORIGINAL image exactly as the user uploaded it, purely so "
    "it can be restored later - you never need or see its S3 key directly. If the user asks to reset the image, "
    "undo all edits, start over, or go back to the original, call `reset_image` (it takes no arguments) - this "
    "discards every edit made so far in this conversation and makes the original upload the current image again. "
    "\n"
    "After you perform image edit ONLY (`rotate`, `flip`, `blur`, `resize`, `crop`, `add_noise`), you MUST ask the user, every single time, whether they'd like to edit something else or "
    "reset all changes back to the original image - never skip this offer, even if you already asked earlier "
    "in this conversation. "
    "After you perform reset via `reset_image`, you MUST ask the user, every single time, whether they'd like to edit something else - never skip this offer, even if you already asked earlier in this conversation. "
    "After showing the annotated image, you MUST ask the user, every single time, whether they'd like to edit something else but dont ask to reset all changes - never skip this offer, even if you already asked earlier in this conversation. "
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
    original_image_s3_key: Optional[str] = None
    image_url: Optional[str] = None
    agent_loop_time_s: float
    iterations: int
    tools_called: List[str]
    context_limit_exceeded: bool
    tool_trace: Optional[str] = None
    tokens_used: TokenUsage  # Added token usage


_current_image_b64: ContextVar[Optional[str]] = ContextVar("current_image_b64", default=None)
_current_image_s3_key: ContextVar[Optional[str]] = ContextVar("current_image_s3_key", default=None)
_current_original_image_s3_key: ContextVar[Optional[str]] = ContextVar("current_original_image_s3_key", default=None)
_current_prediction_id: ContextVar[Optional[str]] = ContextVar("current_prediction_id", default=None)
_current_predicted_image_s3_key: ContextVar[Optional[str]] = ContextVar("current_predicted_image_s3_key", default=None)


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
_MARKDOWN_LINK_RE = re.compile(r"(?<!!)\[([^\]]*)\]\([^)]*\)")
_IMAGE_PLACEHOLDER_RE = re.compile(r"</?image\s*/?>", re.IGNORECASE)


def _strip_markdown_images(text: str) -> str:
    """Remove any markdown image/link syntax or literal <image> placeholder tokens the model
    hallucinates (it has no real image bytes or URL to reference, so ![alt](url), a plain
    [text](url) link, or a bare <image> tag always renders as either a broken image, a dead/
    leaked link, or dead text in the frontend). Plain links are collapsed to their link text
    rather than deleted outright, since that text is usually still a meaningful part of the
    sentence (e.g. "the following link: Annotated Image")."""
    stripped = _MARKDOWN_IMAGE_RE.sub("", text)
    stripped = _MARKDOWN_LINK_RE.sub(r"\1", stripped)
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


def _extract_tool_data(tool_result) -> dict:
    """Parse a tool's structured result for internal bookkeeping (S3 keys, prediction ids)
    that must never be exposed in the LLM-visible `content` (see detect_objects/
    show_annotated_image, which use response_format="content_and_artifact" specifically so
    this internal data never sits in the model's context, where it could get echoed back to
    the user or mistakenly reused as an argument to an editing tool). Handles both a local
    `@tool`'s flat `.artifact` dict and an MCP-discovered tool's `.artifact["structured_content"]`
    nesting, falling back to parsing `.content` as JSON for tools with no artifact at all."""
    artifact = getattr(tool_result, "artifact", None)
    if isinstance(artifact, dict):
        structured = artifact.get("structured_content")
        return structured if isinstance(structured, dict) else artifact
    try:
        return json.loads(_normalize_response_content(tool_result.content))
    except (TypeError, json.JSONDecodeError):
        return {}


def _serialize_tool_trace(new_messages: list) -> Optional[str]:
    """Serialize the AIMessage/ToolMessage sequence produced during a single run_agent
    call so the next HTTP turn can restore the real tool-call trace (which tools ran,
    with what args, and what they returned) instead of losing it to a flattened
    text-only history reconstruction."""
    if not new_messages:
        return None
    events = []
    for m in new_messages:
        if isinstance(m, ToolMessage):
            events.append({
                "role": "tool",
                "name": m.name,
                "tool_call_id": m.tool_call_id,
                "content": m.content if isinstance(m.content, str) else json.dumps(m.content),
            })
        elif isinstance(m, AIMessage):
            events.append({
                "role": "ai",
                "content": _normalize_response_content(m.content),
                "tool_calls": m.tool_calls or [],
            })
    return json.dumps(events)


def _reconstruct_trace_messages(trace_json: str) -> list:
    """Inverse of _serialize_tool_trace: rebuilds the real AIMessage/ToolMessage
    sequence from a previous turn so the LLM sees actual prior tool calls and
    results (e.g. detect_objects box coordinates) rather than a flattened summary."""
    events = json.loads(trace_json)
    restored = []
    for ev in events:
        if ev["role"] == "ai":
            restored.append(AIMessage(content=ev.get("content", ""), tool_calls=ev.get("tool_calls", [])))
        elif ev["role"] == "tool":
            restored.append(ToolMessage(
                content=ev.get("content", ""),
                tool_call_id=ev.get("tool_call_id", ""),
                name=ev.get("name"),
            ))
    return restored


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
def reset_image() -> str:
    """Resets the image back to the original upload, discarding every edit made so far in this
    conversation. Use this when the user asks to reset the image, undo all edits, start over, or
    go back to the original image. Takes no arguments.
    """
    original_key = _current_original_image_s3_key.get()
    current_key = _current_image_s3_key.get()

    if not original_key:
        return json.dumps({"error": "No original image is available to reset to."})
    if current_key == original_key:
        return json.dumps({"status": "already_original"})
    return json.dumps({"status": "reset"})

@tool(response_format="content_and_artifact")
def show_annotated_image():
    """Makes the annotated image (with YOLO bounding boxes drawn on it) ready to show the user.

    Use this tool ONLY when the user explicitly requests to see the visual image or photo.
    Requires a successful prior execution of detect_objects to provide a valid tracking UID.
    The image itself is delivered to the user automatically out-of-band - you will not receive
    a URL or the image bytes here, since you never need to reference them yourself.
    """
    prediction_uid = _current_prediction_id.get()

    if not prediction_uid:
        return json.dumps({
            "error": "No object detection has been performed yet in this session. Run detect_objects first."
        }), {}

    image_url = f"{YOLO_SERVICE_URL}/prediction/{prediction_uid}/image"
    return json.dumps({"status": "ready"}), {"image_url": image_url}

@tool(response_format="content_and_artifact")
def detect_objects():
    """Detect and identify objects in the current image using YOLO object detection.

    Returns a `detections` list with each object's `label`, `score`, and `box` ([x1, y1, x2, y2] pixel
    coordinates), which you can reason over to target a specific object (e.g. "the second dog from the right")
    for the image-editing tools.
    """
    image_s3_key = _current_image_s3_key.get()
    if not image_s3_key:
        return json.dumps({"error": "No image was provided by the user."}), {}

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
        # The full `result` (which includes internal bookkeeping like predicted_image_s3_key)
        # is kept ONLY in the artifact for run_agent's own use - the LLM must never see a raw
        # S3 key for the annotated image, or it can end up passing that key into an editing
        # tool instead of the actual current image (it has no legitimate reason to reference
        # any S3 key from this tool's result at all).
        content = {"detections": result.get("detections", [])}
        return json.dumps(content), result
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
        }), {}
    except httpx.RequestError as exc:
        return json.dumps({
            "error": "YOLO service is unavailable.",
            "detail": str(exc),
        }), {}


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

# Discover the image-editing tools (rotate/flip/blur/resize/crop/add_noise) directly from the
# img-proc-mcp server instead of hand-writing local proxy tools for each of them.
mcp_client = MultiServerMCPClient({
    "img-proc": {
        "url": IMG_PROC_MCP_URL,
        "transport": "http",
    }
})
try:
    img_edit_tools = asyncio.run(mcp_client.get_tools())
except Exception as exc:
    logging.warning(f"Failed to discover tools from img-proc-mcp at {IMG_PROC_MCP_URL}: {exc}")
    img_edit_tools = []
IMAGE_EDIT_TOOL_NAMES = {t.name for t in img_edit_tools}

# Registry: map tool name -> tool function
TOOLS = {
    detect_objects.name: detect_objects,
    show_annotated_image.name: show_annotated_image,
    reset_image.name: reset_image,
    **{t.name: t for t in img_edit_tools},
}

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


async def run_agent(history: list, max_iterations: int = 10) -> dict:
    """
    Simple ReAct loop with an infinite loop safety guard:
      1. Send messages to the LLM.
      2. If the LLM requests tool calls, execute them and append results.
      3. Repeat until the LLM returns a plain text response or max_iterations is reached.
    """
    current_key = _current_image_s3_key.get()
    if current_key:
        image_context = (
            f"\nCURRENT IMAGE: image_s3_key = \"{current_key}\". Every image-editing tool call "
            "(rotate, flip, blur, resize, crop, add_noise) requires this exact `image_s3_key` argument. "
            "Each edit returns a new `output_s3_key` - if you chain multiple edits in the same turn "
            "(e.g. rotate then blur), pass the previous edit's `output_s3_key` as the next call's "
            "`image_s3_key`, not the original."
        )
    else:
        image_context = "\nNo image is currently available - if the user asks for an edit, tell them to upload one first."
    messages = [SystemMessage(content=SYSTEM_PROMPT + image_context)] + history
    initial_len = len(messages)
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
                "original_image_s3_key": _current_original_image_s3_key.get(),
                "image_url": image_url,
                "agent_loop_time_s": loop_time,
                "iterations": iterations,
                "tools_called": tools_called,
                "context_limit_exceeded": context_limit_exceeded,
                "tool_trace": _serialize_tool_trace(messages[initial_len:]),
                "tokens_used": {
                    "input": total_input_tokens,
                    "output": total_output_tokens,
                    "total": total_input_tokens + total_output_tokens
                }
            }

        for tool_call in response.tool_calls:
            tool_name = tool_call.get("name")
            tool_fn = TOOLS[tool_name]

            if tool_name in IMAGE_EDIT_TOOL_NAMES:
                # Never trust whatever image_s3_key the model supplied for an edit - always
                # force it to the actual tracked current image. The model's context can
                # contain multiple S3 keys (e.g. a detection's annotated-image key), and it
                # has occasionally picked the wrong one; forcing this server-side makes "first
                # edit uses the original, later edits build on the previous edit" correct by
                # construction instead of depending on the model getting it right.
                tracked_current_key = _current_image_s3_key.get()
                if tracked_current_key:
                    tool_call = {**tool_call, "args": {**tool_call.get("args", {}), "image_s3_key": tracked_current_key}}

            tool_result = await tool_fn.ainvoke(tool_call)

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
                tool_data = _extract_tool_data(tool_result)
                current_id = tool_data.get("prediction_id") or tool_data.get("prediction_uid")
                if current_id:
                    prediction_id = current_id
                    _current_prediction_id.set(current_id)
                current_predicted_key = tool_data.get("predicted_image_s3_key")
                if current_predicted_key:
                    predicted_image_s3_key = current_predicted_key
                    _current_predicted_image_s3_key.set(current_predicted_key)
            
            if tool_name == "show_annotated_image":
                tool_data = _extract_tool_data(tool_result)
                image_url = tool_data.get("image_url") or image_url
                annotated_image = _fetch_annotated_image(prediction_id) or annotated_image

            if tool_name in IMAGE_EDIT_TOOL_NAMES:
                tool_data = _extract_tool_data(tool_result)
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

            if tool_name == "reset_image":
                original_key = _current_original_image_s3_key.get()
                if original_key and original_key != _current_image_s3_key.get():
                    _current_image_s3_key.set(original_key)
                    last_edited_image_s3_key = original_key
                    # Same as an edit: any annotation computed against the discarded
                    # edits is stale against the restored original image.
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
        "original_image_s3_key": _current_original_image_s3_key.get(),
        "image_url": image_url,
        "agent_loop_time_s": loop_time,
        "iterations": iterations,
        "tools_called": tools_called,
        "context_limit_exceeded": True,
        "tool_trace": _serialize_tool_trace(messages[initial_len:]),
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
                   "http://sawalha.prod.fursa.click:3000",
                   # k8s frontend is only reachable via manual `kubectl port-forward`
                   # (no NodePort/Ingress) - dev forwards to localhost:3000 (already
                   # covered above), prod forwards to localhost:3001. See infra/k8s/README.md.
                   "http://localhost:3001"],
    allow_methods=["POST", "GET"],
    allow_headers=["Content-Type"],
)

CHAT_REQUESTS = Counter("agent_chat_requests_total", "Chat requests by status", ["status"])
CHAT_LATENCY = Histogram("agent_chat_latency_seconds", "Chat request latency in seconds")
CHAT_INPUT_TOKENS = Counter("agent_chat_input_tokens_total", "Cumulative input tokens across chat requests")
CHAT_OUTPUT_TOKENS = Counter("agent_chat_output_tokens_total", "Cumulative output tokens across chat requests")


@app.get("/metrics")
def metrics():
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


class ChatMessage(BaseModel):
    role: str                           # "user" or "assistant"
    content: str
    image_base64: Optional[str] = None  # only on user messages that carry an image
    current_image_s3_key: Optional[str] = None  # echoed back on assistant messages to carry state across turns
    original_image_s3_key: Optional[str] = None  # echoed back on assistant messages to carry state across turns
    prediction_id: Optional[str] = None  # echoed back on assistant messages to carry state across turns
    predicted_image_s3_key: Optional[str] = None  # echoed back on assistant messages to carry state across turns
    tool_trace: Optional[str] = None  # echoed back on assistant messages to restore the real tool-call/result history


class ChatRequest(BaseModel):
    messages: list[ChatMessage]         # full conversation thread, oldest first


@app.post("/chat", response_model=AgentChatResponse)
async def chat(request: ChatRequest):
    start_time = time.perf_counter()
    try:
        result = await _chat_impl(request)
        CHAT_REQUESTS.labels(status="success").inc()
        tokens = result.get("tokens_used") or {}
        CHAT_INPUT_TOKENS.inc(tokens.get("input", 0))
        CHAT_OUTPUT_TOKENS.inc(tokens.get("output", 0))
        return result
    except Exception:
        CHAT_REQUESTS.labels(status="error").inc()
        raise
    finally:
        CHAT_LATENCY.observe(time.perf_counter() - start_time)


async def _chat_impl(request: ChatRequest):
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
            if msg.tool_trace:
                lc_messages.extend(_reconstruct_trace_messages(msg.tool_trace))
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
    original_image_s3_key = None
    prediction_id = None
    predicted_image_s3_key = None
    if new_image_uploaded_this_turn:
        try:
            pred_uid = str(uuid4())
            s3_key = build_original_image_key(chat_id, pred_uid)
            image_s3_key = upload_base64_image(latest_image, s3_key)
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"Failed to upload image to S3: {exc}") from exc
        # A fresh upload is both the original and the current image, and has nothing
        # to do with any prior detection - prediction_id/predicted_image_s3_key stay
        # None so a fresh detect_objects call is required.
        original_image_s3_key = image_s3_key
    else:
        # No new upload this turn - carry forward the most recent image-state
        # snapshot from history (current_image_s3_key/original_image_s3_key/prediction_id/
        # predicted_image_s3_key were all echoed together on the same assistant message).
        # Take all of them from the SAME message rather than independently merging fields
        # from different messages - an edit clears prediction_id/predicted_image_s3_key on
        # its message, and independently falling back to an older message's prediction_id
        # there would resurrect a prediction computed against a since-replaced image.
        for msg in reversed(request.messages):
            if msg.current_image_s3_key:
                image_s3_key = msg.current_image_s3_key
                # Fall back to current_image_s3_key for messages produced before the
                # original_image_s3_key field existed, so old sessions degrade gracefully.
                original_image_s3_key = msg.original_image_s3_key or msg.current_image_s3_key
                prediction_id = msg.prediction_id
                predicted_image_s3_key = msg.predicted_image_s3_key
                break

    token_img = _current_image_b64.set(latest_image)
    token_img_s3 = _current_image_s3_key.set(image_s3_key)
    token_original_img_s3 = _current_original_image_s3_key.set(original_image_s3_key)
    token_pred = _current_prediction_id.set(prediction_id)
    token_predicted_key = _current_predicted_image_s3_key.set(predicted_image_s3_key)
    try:
        agent_payload = await run_agent(lc_messages)
        return agent_payload
    finally:
        _current_image_b64.reset(token_img)
        _current_image_s3_key.reset(token_img_s3)
        _current_original_image_s3_key.reset(token_original_img_s3)
        _current_prediction_id.reset(token_pred)
        _current_predicted_image_s3_key.reset(token_predicted_key)


@app.get("/health")
def health():
    return {"status": "ok"}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
