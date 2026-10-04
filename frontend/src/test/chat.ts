import { HttpResponse } from "msw";

import type { Schemas } from "../api/client";

type Message = Schemas["MessageResponse"];
type Conversation = Schemas["ConversationResponse"];
type Event = Schemas["ChatStreamEvent"];

const AT = "2026-10-04T09:00:00Z";

export const SOURCES: Message["sources"] = [
  {
    kind: "document",
    material_id: "m-1",
    attachment_id: null,
    title: "Положение о командировках.docx",
    heading_path: ["Положение о командировках", "2. Суточные"],
    position: 3,
    content:
      "Положение о командировках > 2. Суточные\nСуточные при командировках по России — 700 рублей в сутки.",
    source_url: "https://portal.example.ru/docs/42",
  },
  {
    kind: "document",
    material_id: "m-2",
    attachment_id: null,
    title: "Приказ о суточных",
    heading_path: [],
    position: 0,
    content: "Приказ о суточных\nЗа рубеж — 2500 рублей.",
    source_url: null,
  },
];

export function question(overrides: Partial<Message> = {}): Message {
  return {
    id: "q-1",
    parent_id: null,
    role: "user",
    content: "Какие суточные?",
    status: "complete",
    origin: null,
    sources: [],
    attachments: [],
    error_code: null,
    feedback: null,
    feedback_reason: null,
    feedback_comment: null,
    siblings: ["q-1"],
    created_at: AT,
    ...overrides,
  };
}

export function reply(overrides: Partial<Message> = {}): Message {
  return question({
    id: "a-1",
    parent_id: "q-1",
    role: "assistant",
    content: "Суточные по России — 700 рублей [1]. За рубеж — 2500 рублей [2].",
    origin: "documents",
    sources: SOURCES,
    siblings: ["a-1"],
    ...overrides,
  });
}

export function conversation(
  messages: Message[] = [question(), reply()],
  overrides: Partial<Conversation> = {},
): Conversation {
  return {
    id: "c-1",
    title: "Какие суточные?",
    pinned: false,
    shared: false,
    created_at: AT,
    updated_at: AT,
    current_message_id: messages.at(-1)?.id ?? null,
    messages,
    share: null,
    ...overrides,
  };
}

export function summary(overrides: Partial<Schemas["ConversationSummary"]> = {}) {
  return {
    id: "c-1",
    title: "Какие суточные?",
    pinned: false,
    shared: false,
    created_at: AT,
    updated_at: new Date().toISOString(),
    ...overrides,
  };
}

export function sse(events: Event[]): string {
  return events.map((event) => `data: ${JSON.stringify(event)}\n\n`).join("");
}

/** Поток ответа целиком (text/event-stream). */
export function eventStream(events: Event[] | ReadableStream<Uint8Array>) {
  const body = Array.isArray(events) ? sse(events) : events;
  return new HttpResponse(body, { headers: { "Content-Type": "text/event-stream" } });
}

/** Поток, который тест дописывает сам: остановка посреди ответа. */
export function controlledStream() {
  const encoder = new TextEncoder();
  let controller!: ReadableStreamDefaultController<Uint8Array>;
  const stream = new ReadableStream<Uint8Array>({
    start(c) {
      controller = c;
    },
  });
  return {
    stream,
    push: (...events: Event[]) => controller.enqueue(encoder.encode(sse(events))),
    close: () => controller.close(),
  };
}

/** Обычный ход: start, поиск, текст двумя кусками, итог. */
export function answerEvents(
  answer: Message = reply(),
  {
    conversationTitle = "Какие суточные?",
    asked = question(),
  }: { conversationTitle?: string; asked?: Message } = {},
): Event[] {
  const half = Math.ceil(answer.content.length / 2);
  return [
    {
      type: "start",
      conversation: summary({ id: "c-1", title: conversationTitle }),
      question: asked,
      answer: { ...answer, status: "generating", content: "", sources: [] },
    },
    { type: "stage", stage: "searching" },
    { type: "stage", stage: "writing" },
    { type: "delta", text: answer.content.slice(0, half) },
    { type: "delta", text: answer.content.slice(half) },
    { type: "done", answer: { ...answer, siblings: [] }, diagnostics: null },
  ];
}
