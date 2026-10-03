import { api, unwrap, type Schemas } from "../api/client";
import { readEvents, type StreamEvent } from "./sse";

export type Message = Schemas["MessageResponse"];
export type Conversation = Schemas["ConversationResponse"];
export type Summary = Schemas["ConversationSummary"];
export type Source = Schemas["MessageSourceResponse"];
export type Attachment = Schemas["AttachmentResponse"];
export type Origin = Schemas["AnswerOrigin"];
export type FeedbackReason = NonNullable<Message["feedback_reason"]>;

/** Как в схеме бэкенда (MAX_QUESTION_CHARS). */
export const MAX_QUESTION = 4000;
export const MAX_ATTACHMENTS = 5;

export type Turn =
  | { kind: "new"; question: string; attachmentIds: string[] }
  | {
      kind: "ask";
      conversationId: string;
      parentId: string | null;
      question: string;
      attachmentIds: string[];
    }
  | { kind: "regenerate"; conversationId: string; questionId: string };

/**
 * Начать ход и читать его события. Ошибка до потока (лимит, 409, 422) —
 * ApiError из unwrap; обрыв соединения посреди потока — исключение из
 * чтения: ответ при этом дописывается на сервере.
 */
export async function streamTurn(
  turn: Turn,
  onEvent: (event: StreamEvent) => void,
  signal: AbortSignal,
): Promise<void> {
  const options = { parseAs: "stream" as const, signal };
  const stream =
    turn.kind === "new"
      ? await unwrap(
          api.POST("/api/v1/conversations", {
            ...options,
            body: { question: turn.question, attachment_ids: turn.attachmentIds },
          }),
        )
      : turn.kind === "ask"
        ? await unwrap(
            api.POST("/api/v1/conversations/{conversation_id}/messages", {
              ...options,
              params: { path: { conversation_id: turn.conversationId } },
              body: {
                question: turn.question,
                parent_id: turn.parentId,
                attachment_ids: turn.attachmentIds,
              },
            }),
          )
        : await unwrap(
            api.POST("/api/v1/conversations/{conversation_id}/messages/{message_id}/regenerate", {
              ...options,
              params: {
                path: { conversation_id: turn.conversationId, message_id: turn.questionId },
              },
            }),
          );
  if (!stream) throw new Error("empty stream");
  for await (const event of readEvents(stream)) onEvent(event);
}

export function stopAnswer(conversationId: string, messageId: string) {
  return unwrap(
    api.POST("/api/v1/conversations/{conversation_id}/messages/{message_id}/stop", {
      params: { path: { conversation_id: conversationId, message_id: messageId } },
    }),
  );
}

export function fetchConversation(id: string) {
  return unwrap(
    api.GET("/api/v1/conversations/{conversation_id}", {
      params: { path: { conversation_id: id } },
    }),
  );
}

/** Путь до parentId включительно: новая ветка начинается после него. */
export function pathUpTo(messages: Message[], parentId: string | null): Message[] {
  if (parentId === null) return [];
  const index = messages.findIndex((message) => message.id === parentId);
  return index < 0 ? messages : messages.slice(0, index + 1);
}

export function shareUrl(token: string): string {
  return `${window.location.origin}/shared/${token}`;
}
