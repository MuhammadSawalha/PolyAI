import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { cn } from "@/lib/utils";
import type { ChatMessage } from "@/lib/types";

export default function MessageBubble({ message }: { message: ChatMessage }) {
  const isUser = message.role === "user";  
  return (
    <div className={cn("flex", isUser ? "justify-end" : "justify-start")}>
      <div
        className={cn(
          "max-w-[80%] rounded-2xl px-4 py-3 text-sm shadow-sm",
          isUser
            ? "bg-primary text-primary-foreground rounded-br-sm"
            : "bg-muted text-foreground rounded-bl-sm border border-border/50"
        )}
      >
        {message.image_base64 && (
          <img
            src={`data:image/jpeg;base64,${message.image_base64}`}
            alt="uploaded"
            className="mb-2 max-h-48 rounded-lg object-contain"
          />
        )}
        {isUser ? (
          <p className="whitespace-pre-wrap">{message.content}</p>
        ) : (
          <div className="prose prose-sm max-w-none dark:prose-invert prose-p:my-1 prose-ul:my-1 prose-li:my-0">
            <ReactMarkdown remarkPlugins={[remarkGfm]}>
              {message.content}
            </ReactMarkdown>
          </div>
        )}
        {/* Force clear rendering frame container */}
        {message.annotated_image && message.annotated_image.length > 0 ? (
          <div className="mt-3 block w-full max-w-sm overflow-hidden rounded-lg border border-border bg-muted/40 shadow-sm">
            <img
              src={
                message.annotated_image.startsWith("data:")
                  ? message.annotated_image
                  : `data:image/jpeg;base64,${message.annotated_image}`
              }
              alt="Annotated Base64 Output"
              className="w-full h-auto object-contain block"
            />
          </div>
        ) : (
          message.image_url && (
            <div className="mt-3 block w-full max-w-sm overflow-hidden rounded-lg border border-border bg-muted/40 shadow-sm">
              <img
                src={message.image_url}
                alt="Annotated Detection Asset Source"
                className="w-full h-auto min-h-[150px] object-contain block"
                onError={(e) => {
                  console.error("The browser blocked loading this asset link directly:", message.image_url);
                }}
              />
            </div>
          )
        )}
        {message.edited_image && message.edited_image.length > 0 && (
          <div className="mt-3 block w-full max-w-sm overflow-hidden rounded-lg border border-border bg-muted/40 shadow-sm">
            <img
              src={
                message.edited_image.startsWith("data:")
                  ? message.edited_image
                  : `data:image/png;base64,${message.edited_image}`
              }
              alt="Edited Image Output"
              className="w-full h-auto object-contain block"
            />
          </div>
        )}
      </div>
    </div>
  );
}