export interface ChatMessage {
  role: "user" | "assistant";
  content: string;
  image_base64?: string;
  annotated_image?: string | null;
  edited_image?: string | null;
  current_image_s3_key?: string | null;
  original_image_s3_key?: string | null;
  prediction_id?: string | null;
  predicted_image_s3_key?: string | null;
  image_url?: string;
  tool_trace?: string | null;
}
