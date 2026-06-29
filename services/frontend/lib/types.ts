export interface ChatMessage {
  role: "user" | "assistant";
  content: string;
  image_base64?: string;
  annotated_image?: string | null;
  image_url?: string;
}
