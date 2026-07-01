import base64
import json
import logging
import os
import posixpath
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
    "You are an AI vision assistant. You help users analyze images via tools. "
    "To analyze an image, you must run `detect_objects`, followed by `show_annotated_image` if the user requests to show the annotated image. "
    "CRITICAL OUTPUT RULES:"
    "1. If the user did not ask to see the annotated image, do not include it in your response. but you can ask him if he wants to see it. "
    "2. If the user asked to see the annotated image, do not print raw image URLs just let the image appear directly. "
    "3. You MUST read the tool output data from `detect_objects` and write a detailed, natural paragraph summary breaking down exactly what items were found. "
    "4. Do not include raw XML tags like `<thinking>` or `</thinking>` in your text reply. "
    "if the user said yes for the annotated image, you must include it in your response. not the link, just the image itself. "
)

class TokenUsage(BaseModel):
    input: int
    output: int
    total: int

class AgentChatResponse(BaseModel):
    response: str
    prediction_id: Optional[str] = None
    annotated_image: Optional[str] = None
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
    """Detect and identify objects in the image provided by the user using YOLO object detection."""
    image_s3_key = _current_image_s3_key.get()
    if not image_s3_key:
        return json.dumps({"error": "No image was provided by the user."})

    try:
        with httpx.Client(timeout=30.0) as client:
            response = client.post(f"{YOLO_SERVICE_URL}/predict", json={"image_s3_key": image_s3_key})
            response.raise_for_status()
        return json.dumps(response.json())
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


# Registry: map tool name -> tool function
TOOLS = {
    detect_objects.name: detect_objects,
    show_annotated_image.name: show_annotated_image,
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
    annotated_image: Optional[str] = None
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
            return {
                "response": _normalize_response_content(response.content),
                "prediction_id": prediction_id,
                "annotated_image": annotated_image,
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
                predicted_image_s3_key = tool_data.get("predicted_image_s3_key")
                if predicted_image_s3_key:
                    _current_predicted_image_s3_key.set(predicted_image_s3_key)
            
            if tool_name == "show_annotated_image":
                tool_data = json.loads(tool_result.content)
                image_url = tool_data.get("image_url") or image_url
                annotated_image = _fetch_annotated_image(prediction_id) or annotated_image

    loop_time = round(time.perf_counter() - start_time, 4)
    error_msg = f"⚠️ Agent stopped automatically: Reached safety limit of {max_iterations} iterations without resolving."
    logging.warning(error_msg)
    return {
        "response": error_msg,
        "prediction_id": prediction_id,
        "annotated_image": annotated_image,
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

    image_s3_key = None
    if latest_image:
        try:
            pred_uid = str(uuid4())
            s3_key = build_original_image_key(chat_id, pred_uid)
            image_s3_key = upload_base64_image(latest_image, s3_key)
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"Failed to upload image to S3: {exc}") from exc

    token_img = _current_image_b64.set(latest_image)
    token_img_s3 = _current_image_s3_key.set(image_s3_key)
    token_pred = _current_prediction_id.set(None) # Reset local state per request context
    token_predicted_key = _current_predicted_image_s3_key.set(None)
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
