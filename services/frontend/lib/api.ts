import type { ChatMessage } from "./types";

const AGENT_URL = process.env.NEXT_PUBLIC_AGENT_URL ?? "http://localhost:8000";

export interface SendMessageResult {
  response: string;
  annotated_image: string | null;
  edited_image: string | null;
  current_image_s3_key: string | null;
  prediction_id: string | null;
  predicted_image_s3_key: string | null;
  image_url: string | null;
  tool_trace: string | null;
}

export async function sendMessage(
  messages: ChatMessage[]
): Promise<SendMessageResult> {
  const res = await fetch(`${AGENT_URL}/chat`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ messages }),
  });
  if (!res.ok) {
    const text = await res.text().catch(() => res.statusText);
    throw new Error(text || res.statusText);
  }
  const data = await res.json();
  return {
    response: data.response as string,
    annotated_image: data.annotated_image ?? null,
    edited_image: data.edited_image ?? null,
    current_image_s3_key: data.current_image_s3_key ?? null,
    prediction_id: data.prediction_id ?? null,
    predicted_image_s3_key: data.predicted_image_s3_key ?? null,
    image_url: data.image_url ?? null,
    tool_trace: data.tool_trace ?? null,
  };
}