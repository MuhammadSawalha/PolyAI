import base64
import io
import json
import logging
import os
import time
from contextvars import ContextVar
from typing import List, Optional

from dotenv import load_dotenv
load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logging.getLogger("langchain").setLevel(logging.DEBUG)
logging.getLogger("langchain_core").setLevel(logging.DEBUG)

import httpx
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from langchain.chat_models import init_chat_model
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import tool
from pydantic import BaseModel

YOLO_SERVICE_URL = os.environ.get("YOLO_SERVICE_URL", "http://localhost:8080")
MODEL = os.environ.get("MODEL")

# Text-only models
ALLOWED_MODELS = {
    "openai:gpt-5.4-mini",
    "anthropic:claude-haiku-4-5",
    "google_genai:gemini-2.5-flash",
}

if MODEL not in ALLOWED_MODELS:
    allowed_list = "\n  ".join(sorted(ALLOWED_MODELS))
    raise SystemExit(
        f"\n[ERROR] MODEL='{MODEL}' is not allowed.\n"
        f"Set MODEL in your .env to one of the supported text-only models:\n  {allowed_list}\n"
    )

SYSTEM_PROMPT = (
    "You are an AI vision assistant. You help users understand and analyze images. "
    "Use the available tools to extract information from images. "
)


class AgentChatResponse(BaseModel):
    response: str
    prediction_id: Optional[str] = None
    annotated_image: Optional[str] = None
    agent_loop_time_s: float
    iterations: int
    tools_called: List[str]
    context_limit_exceeded: bool


_current_image_b64: ContextVar[Optional[str]] = ContextVar("current_image_b64", default=None)


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


def _extract_prediction_id(tool_output: object) -> Optional[str]:
    if isinstance(tool_output, str):
        try:
            payload = json.loads(tool_output)
        except json.JSONDecodeError:
            return None
    elif isinstance(tool_output, dict):
        payload = tool_output
    else:
        return None

    if isinstance(payload, dict):
        return payload.get("prediction_uid") or payload.get("prediction_id")
    return None


def _fetch_annotated_image(prediction_id: Optional[str]) -> Optional[str]:
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
def detect_objects() -> str:
    """Detect and identify objects in the image provided by the user using YOLO object detection."""
    image_b64 = _current_image_b64.get()
    if not image_b64:
        return json.dumps({"error": "No image was provided by the user."})

    image_bytes = base64.b64decode(image_b64)
    with httpx.Client(timeout=30.0) as client:
        response = client.post(
            f"{YOLO_SERVICE_URL}/predict",
            files={"file": ("image.jpg", io.BytesIO(image_bytes), "image/jpeg")},
        )
        response.raise_for_status()
    return json.dumps(response.json())


# Registry: map tool name -> tool function
TOOLS = {
    detect_objects.name: detect_objects
}

llm = init_chat_model(MODEL, temperature=0)
llm_with_tools = llm.bind_tools(list(TOOLS.values()))

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
    start_time = time.perf_counter()

    while iterations < max_iterations:
        iterations += 1
        logging.info(f"🤖 Agent iteration {iterations}/{max_iterations}")

        response: AIMessage = llm_with_tools.invoke(messages)
        messages.append(response)

        if not response.tool_calls:
            loop_time = round(time.perf_counter() - start_time, 4)
            return {
                "response": _normalize_response_content(response.content),
                "prediction_id": prediction_id,
                "annotated_image": annotated_image,
                "agent_loop_time_s": loop_time,
                "iterations": iterations,
                "tools_called": tools_called,
                "context_limit_exceeded": False,
            }

        for tool_call in response.tool_calls:
            tool_name = tool_call.get("name")
            tool_fn = TOOLS[tool_name]
            tool_result = tool_fn.invoke(tool_call)
            tool_output = tool_result.content if hasattr(tool_result, "content") else str(tool_result)
            if not hasattr(tool_result, "content"):
                tool_result = ToolMessage(content=tool_output, tool_call_id=tool_call.get("id", ""))
            messages.append(tool_result)
            if tool_name:
                tools_called.append(tool_name)
            if tool_name == "detect_objects":
                prediction_id = _extract_prediction_id(tool_output)
                if prediction_id:
                    annotated_image = _fetch_annotated_image(prediction_id)

    loop_time = round(time.perf_counter() - start_time, 4)
    error_msg = f"⚠️ Agent stopped automatically: Reached safety limit of {max_iterations} iterations without resolving."
    logging.warning(error_msg)
    return {
        "response": error_msg,
        "prediction_id": prediction_id,
        "annotated_image": annotated_image,
        "agent_loop_time_s": loop_time,
        "iterations": iterations,
        "tools_called": tools_called,
        "context_limit_exceeded": True,
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

    token = _current_image_b64.set(latest_image)
    try:
        agent_payload = run_agent(lc_messages)
        return agent_payload
    finally:
        _current_image_b64.reset(token)


@app.get("/health")
def health():
    return {"status": "ok"}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
