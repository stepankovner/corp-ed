import { describe, expect, it } from "vitest";

import { readEvents, type StreamEvent } from "./sse";

function streamOf(...parts: string[]): ReadableStream<Uint8Array> {
  const encoder = new TextEncoder();
  return new ReadableStream({
    start(controller) {
      for (const part of parts) controller.enqueue(encoder.encode(part));
      controller.close();
    },
  });
}

async function collect(stream: ReadableStream<Uint8Array>): Promise<StreamEvent[]> {
  const out: StreamEvent[] = [];
  for await (const event of readEvents(stream)) out.push(event);
  return out;
}

describe("readEvents", () => {
  it("splits blocks across chunk boundaries and skips pings", async () => {
    const events = await collect(
      streamOf(
        'data: {"type":"stage","stage":"searching"}\n\n: ping\n\ndata: {"type":"del',
        'ta","text":"Отпу',
        'ск"}\r\n\r\ndata: {"type":"reset"}\n\n',
      ),
    );
    expect(events).toEqual([
      { type: "stage", stage: "searching" },
      { type: "delta", text: "Отпуск" },
      { type: "reset" },
    ]);
  });

  it("decodes multibyte characters split between chunks", async () => {
    const bytes = new TextEncoder().encode('data: {"type":"delta","text":"ё"}\n\n');
    const stream = new ReadableStream<Uint8Array>({
      start(controller) {
        controller.enqueue(bytes.slice(0, 30));
        controller.enqueue(bytes.slice(30));
        controller.close();
      },
    });
    expect(await collect(stream)).toEqual([{ type: "delta", text: "ё" }]);
  });

  it("reads a last event without trailing blank line", async () => {
    expect(await collect(streamOf('data: {"type":"reset"}'))).toEqual([{ type: "reset" }]);
  });
});
