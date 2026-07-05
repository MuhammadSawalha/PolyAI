---
name: img-proc-mcp
description: Use this skill when working on the img-proc-mcp image-editing MCP server (services/img-proc-mcp) or on the agent's integration with it (services/agent's rotate/flip/blur/resize/crop/add_noise tools, mcp_client.py, and the detect_objects bbox extension). This includes adding/changing image-editing tools, changing bbox-scoped edit semantics, wiring new MCP tools into the agent's ReAct loop, or touching the ContextVar-based image-chaining pattern.

---

# img-proc-mcp: Image Editing MCP Server & Agent Integration

## Overview

`services/img-proc-mcp` is an MCP-over-HTTP server (built with `fastmcp`) exposing image-editing tools: `rotate`, `flip`, `blur`, `resize`, `crop`, `add_noise`. `services/agent` is its only client: it wraps each MCP tool as a local LangChain `@tool` so the LLM can fulfill requests like "rotate the entire image 90 degrees" (whole-image edit) or "blur the second dog from the right" (object-scoped edit, which requires first calling the existing `detect_objects` tool to get bounding boxes).

These two services are not independently useful — a server with no client, or client tools with no server, don't accomplish anything — so they're covered by one skill.

The architecture rests on three pillars:

1. **S3 keys in, S3 keys out — never raw image bytes over MCP or over the LLM-facing hop.** Every tool takes `image_s3_key: str` and returns `{"output_s3_key": str, "width": int, "height": int}`. This matches how `yolo` and `agent` already move images (S3 object keys, never base64 bodies) and keeps large blobs out of the MCP/LLM path.
2. **The LLM never sees image bytes, but bbox coordinates and labels are plain metadata it may reason over.** Per the repo-wide `AGENTS.md` rule ("the LLM never sees image data"), no pixel data ever enters a LangChain message or tool argument. But a bounding box (`[x1, y1, x2, y2]`) or a label string is just a number/string, not image data — the LLM is expected to read `detect_objects`' `detections` list and reason over `box` coordinates itself (e.g. sort by `box[0]` to find "the Nth `<label>` from the right").
3. **Editing tools chain within a turn via a ContextVar, not explicit LLM-passed keys.** The LLM never has to know or pass an S3 key — `_current_working_image_s3_key` in `services/agent/app.py` tracks "the image currently being worked on," updated after every successful edit so a sequence like rotate → blur operates on the rotated result automatically.

---

## Core Architecture Principles

- **S3-key contract, not base64** — Every `img-proc-mcp` tool signature is `(image_s3_key: str, ...params, bbox: Optional[List[float]] = None) -> dict` (except `crop`, where `bbox` is required) returning `{"output_s3_key", "width", "height"}`. Never add a base64 image parameter or return value to a tool.
- **bbox semantics are fixed, do not casually change them:**
  - `bbox=None` → whole-image edit.
  - `bbox=[x1,y1,x2,y2]` given → crop that region, apply the edit, paste back into the full-size image at `(x1,y1)` — **except** `crop`, whose whole point is to return just the cropped region, uncomposited.
  - **`rotate` with a bbox only accepts angles that are multiples of 90** (raises `ValueError` otherwise). Whole-image rotation accepts any angle. This avoids the corner-clipping/fill-color ambiguity of rotating a rectangle by an arbitrary angle in place. Internally, bbox-scoped 90/180/270 rotation must use `region.transpose(Image.Transpose.ROTATE_90/180/270)`, **not** `region.rotate(angle, expand=False)` — the latter fills corners with black on non-square regions even at exact 90° (verified bug during initial implementation). `_apply_in_place`'s automatic "resize back to the original footprint if the transform changed the region's size" handles the width/height swap from a 90°/270° transpose.
  - **`resize` with a bbox stretches** the cropped region to the new `(width, height)` and pastes it back at the same `(x1, y1)` offset — the region's footprint on the canvas literally changes size (may overlap neighboring content or extend past the original box). This is a deliberate, confirmed design choice, not an oversight.
- **Output S3 key naming**: `build_edited_image_key(image_s3_key, tool_name)` in `services/img-proc-mcp/app.py` derives `chat_id`/`prediction_id` from the *input* key's first two path segments (mirrors `build_original_image_key`/yolo's `_build_predicted_image_key` convention) so chained edits keep landing under the same `{chat_id}/{prediction_id}/edited/` prefix, and generates a fresh UUID-based filename per call so outputs never collide.
- **No shared S3 lib** — `services/img-proc-mcp/s3.py` is an intentional byte-for-byte copy of `services/yolo/s3.py` / `services/agent/s3.py`. This repo duplicates this small wrapper per service rather than factoring out a shared package; do not "fix" that by extracting one.
- **Agent-side MCP client stays minimal and explicit** — `services/agent/mcp_client.py`'s `call_mcp_tool(tool_name, arguments)` is a synchronous wrapper (`asyncio.run(...)`) around `fastmcp.Client`, deliberately not `langchain-mcp-adapters`/`MultiServerMCPClient`. Per `AGENTS.md`'s "keep it explicit, not magic" / "no high-level agent framework black boxes" rule, the manual `run_agent()` ReAct loop and the plain `TOOLS` dict registry must be extended, never replaced by a framework wrapper.
- **`detect_objects` extension must not touch YOLO's API contract** — `services/agent/app.py`'s `detect_objects` tool now also calls `GET /prediction/{uid}` after `POST /predict` succeeds, merging a `detections` list (`id`, `label`, `score`, `box` parsed via `_parse_bbox`) into what it returns to the LLM. This is agent-side only. Per the `yolo-api-data-layer` skill's preservation rules, **never** change YOLO's routes, status codes, or response schemas to support this — `GET /prediction/{uid}` already returns `detection_objects` with a `box` field (a JSON-stringified `[x1,y1,x2,y2]` list, e.g. `"[100.5, 150.2, 200.1, 250.9]"` — parse with `json.loads`, not a tensor-string regex).
- **Image-edit tool functions are plain functions, testable without the MCP transport** — every `@mcp.tool()`-decorated function in `services/img-proc-mcp/app.py` remains directly callable as a plain Python function (fastmcp's decorator does not wrap/replace it). Test business logic by calling functions directly; reserve MCP-transport tests (`fastmcp.Client(app.mcp)` in-memory) for confirming tool registration/wiring only.

---

## Preservation Rules

### ✅ DO Modify / Extend

- `services/img-proc-mcp/app.py` — add new image-editing tools following the existing `(image_s3_key, ...params, bbox=None) -> dict` signature pattern and the `_download_image` / `_apply_in_place` / `_finish` helper pipeline.
- `services/agent/app.py` — add new `@tool` wrappers to `TOOLS` for any new img-proc-mcp tool, following the `_call_image_edit_tool(tool_name, arguments)` pattern (pulls `image_s3_key` from `_current_working_image_s3_key`, calls `call_mcp_tool`, returns an error JSON on failure — never raises to the LLM).
- `SYSTEM_PROMPT` — extend with guidance for new tools/behaviors, following the existing bbox-reasoning instructions.
- `run_agent()`'s tool-dispatch loop — extend the `if tool_name in IMAGE_EDIT_TOOL_NAMES:` bookkeeping branch if new edit tools are added; keep updating `_current_working_image_s3_key` and `last_edited_image_s3_key` so chaining and the final `edited_image` response field keep working.

### ❌ DO NOT Modify

- **YOLO's routes/status codes/response schemas** — `detect_objects`'s extension only adds a second read-only GET call; it must never require a YOLO API change.
- **The S3-key-in/S3-key-out contract** — do not add base64 image parameters to any img-proc-mcp tool or agent wrapper.
- **The "LLM never sees image bytes" rule** — bbox arrays/labels are fine; never put base64 image content into a `HumanMessage`, tool argument, or `SYSTEM_PROMPT`.
- **The manual ReAct loop / `TOOLS` dict pattern** — do not introduce `create_react_agent`, `AgentExecutor`, `MultiServerMCPClient`, or similar framework-level black boxes.
- **`crop`'s non-compositing behavior** — it is the one tool that returns just the cropped region; do not make it paste back into the full image.
- **The 90°-multiple restriction on bbox-scoped `rotate`** — do not silently allow arbitrary angles with bbox (would require a fill-color decision that hasn't been made).

---

## Common Tasks

### Task: Add a new image-editing tool (e.g. `sharpen`)

1. In `services/img-proc-mcp/app.py`: add `@mcp.tool() def sharpen(image_s3_key: str, ...params, bbox: Optional[List[float]] = None) -> dict`, using `_download_image`, `_apply_in_place` (unless it's a `crop`-like non-compositing tool), and `_finish(img, image_s3_key, "sharpen")`.
2. In `services/img-proc-mcp/tests/test_tools.py`: add whole-image happy path, bbox-scoped happy path (assert pixels outside bbox unchanged), and error-case tests, matching the existing per-tool test blocks.
3. In `services/agent/app.py`: add a `@tool def sharpen(...) -> str: return _call_image_edit_tool("sharpen", {...})`, add it to `TOOLS`, add it to `IMAGE_EDIT_TOOL_NAMES`, and extend `SYSTEM_PROMPT` if its bbox semantics need explanation.
4. In `services/agent/tests/test_agent.py`: add it to the `IMAGE_EDIT_TOOLS` parametrized dict so it's automatically covered by the success/no-image/mcp-failure test matrix.

### Task: Change bbox-scoped semantics for an existing tool

Update the tool's implementation and docstring in `services/img-proc-mcp/app.py`, update its README table row, update `SYSTEM_PROMPT` in `services/agent/app.py` if the LLM-facing guidance changes, and update/add tests asserting the new pixel-level behavior (outside-bbox-unchanged assertions are the key regression guard).

### Task: Debug an object-scoped edit not targeting the right detection

Check `detect_objects`'s `detections` list is populated (requires `prediction_uid` in the `/predict` response) and that `SYSTEM_PROMPT`'s sort-direction guidance (`box[0]` ascending/descending for left/right, `box[1]` for top/bottom) matches what the LLM actually did — this is LLM reasoning over plain numbers, not a code bug, most of the time.

---

## Verification

- `services/img-proc-mcp`: `pytest --cov=app --cov-report=term-missing` (target: 100%, matches `yolo-api-data-layer`'s bar for its own service).
- `services/agent`: `pytest --cov=app --cov-report=term-missing` (matches the pre-existing coverage baseline; the `ALLOWED_MODELS`/capability-check/`__main__` gaps are pre-existing and out of scope).
- End-to-end: upload an image via the frontend, try a whole-image edit ("rotate the entire image 90 degrees") and an object-scoped edit ("blur the second dog from the right") in the same conversation, and confirm the edited image renders via the `edited_image` response field.

## Prompts That Activate This Skill

- "Add a `sharpen` tool to img-proc-mcp"
- "Why does rotating a specific detected object leave black corners?"
- "Change resize so bbox-scoped resize does X instead"
- "The agent isn't chaining rotate and blur correctly"
- "Add a new image-editing tool and wire it into the agent"
- "Why can't the LLM see the bbox coordinates — doesn't that violate the no-image-data rule?"
