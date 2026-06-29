import type { ChatMessage } from "./types";

const AGENT_URL = process.env.NEXT_PUBLIC_AGENT_URL ?? "http://localhost:8000";

export interface SendMessageResult {
  response: string;
  annotated_image: string | null;
  image_url: string | null;
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
    image_url: data.image_url ?? null,
  };
}