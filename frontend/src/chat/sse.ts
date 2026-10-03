import type { Schemas } from "../api/client";

export type StreamEvent = Schemas["ChatStreamEvent"];

/**
 * События потока ответа (text/event-stream): блоки через пустую строку,
 * полезная нагрузка — строки «data: …». Комментарии («: ping» — сервер
 * держит соединение) пропускаются.
 */
export async function* readEvents(stream: ReadableStream<Uint8Array>): AsyncGenerator<StreamEvent> {
  const reader = stream.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  try {
    for (;;) {
      const { value, done } = await reader.read();
      buffer += done ? decoder.decode() : decoder.decode(value, { stream: true });
      buffer = buffer.replace(/\r\n/g, "\n");
      let end = buffer.indexOf("\n\n");
      while (end >= 0) {
        const event = parse(buffer.slice(0, end));
        buffer = buffer.slice(end + 2);
        if (event) yield event;
        end = buffer.indexOf("\n\n");
      }
      if (done) break;
    }
    const tail = parse(buffer);
    if (tail) yield tail;
  } finally {
    reader.releaseLock();
  }
}

function parse(block: string): StreamEvent | null {
  const data = block
    .split("\n")
    .filter((line) => line.startsWith("data:"))
    .map((line) => line.slice(5).replace(/^ /, ""))
    .join("\n");
  return data ? (JSON.parse(data) as StreamEvent) : null;
}
