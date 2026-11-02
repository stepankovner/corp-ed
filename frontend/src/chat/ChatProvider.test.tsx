import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, renderHook } from "@testing-library/react";
import type { ReactNode } from "react";
import { expect, it, vi } from "vitest";

import { answerEvents, question, reply } from "../test/chat";
import { streamTurn } from "./api";
import { ChatProvider } from "./ChatProvider";
import type { StreamEvent } from "./sse";
import { useChat } from "./store";

vi.mock("./api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("./api")>()),
  streamTurn: vi.fn(),
}));

function pendingStream() {
  let emit!: (event: StreamEvent) => void;
  let close!: () => void;
  const closed = new Promise<void>((resolve) => {
    close = resolve;
  });
  vi.mocked(streamTurn).mockImplementationOnce((_turn, onEvent) => {
    emit = onEvent;
    return closed;
  });
  return { push: (event: StreamEvent) => emit(event), close };
}

it("закрытие reader прежнего ответа после done не стирает следующий живой ответ", async () => {
  const first = pendingStream();
  const next = pendingStream();
  const client = new QueryClient();
  const { result } = renderHook(useChat, {
    wrapper: ({ children }: { children: ReactNode }) => (
      <QueryClientProvider client={client}>
        <ChatProvider>{children}</ChatProvider>
      </QueryClientProvider>
    ),
  });
  await act(async () => {
    const accepted = result.current.send({
      kind: "ask",
      conversationId: "c-1",
      parentId: null,
      question: "Первый вопрос",
      attachmentIds: [],
    });
    const events = answerEvents();
    first.push(events[0]!);
    first.push(events.at(-1)!);
    await accepted;
  });
  expect(result.current.live["c-1"]).toBeUndefined();
  await act(async () => {
    const accepted = result.current.send({
      kind: "ask",
      conversationId: "c-1",
      parentId: "a-1",
      question: "Следующий вопрос",
      attachmentIds: [],
    });
    next.push(
      answerEvents(reply({ id: "a-2", parent_id: "q-2" }), {
        asked: question({ id: "q-2", parent_id: "a-1", content: "Следующий вопрос" }),
      })[0]!,
    );
    await accepted;
  });
  expect(result.current.live["c-1"]?.answerId).toBe("a-2");
  // EOF предыдущего HTTP-потока приходит уже после start нового хода.
  await act(async () => first.close());
  expect(result.current.live["c-1"]?.answerId).toBe("a-2");
  await act(async () => next.push({ type: "delta", text: "Новый ответ" }));
  expect(result.current.live["c-1"]?.text).toBe("Новый ответ");
  await act(async () => next.close());
  client.clear();
});
