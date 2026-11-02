import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { afterEach, describe, expect, it, vi } from "vitest";

import { api } from "../api/client";
import {
  answerEvents,
  controlledStream,
  conversation,
  eventStream,
  question,
  reply,
} from "../test/chat";
import { me } from "../test/fixtures";
import { renderApp } from "../test/render";
import { server } from "../test/server";
import type { Attachment } from "./api";

const FIRST: Attachment = { id: "file-1", filename: "first.txt", size: 5, tokens: 2 };
const NEXT: Attachment = { id: "file-2", filename: "next.txt", size: 4, tokens: 2 };

afterEach(() => vi.restoreAllMocks());

function signedInWithUploads() {
  let uploads = 0;
  // jsdom FormData несовместима с Node fetch (как в тестах аватара).
  // Подменяем только загрузку; сам вопрос и SSE проходят через MSW.
  const post = api.POST;
  vi.spyOn(api, "POST").mockImplementation((path, options) => {
    if (path !== "/api/v1/attachments") return post(path, options);
    uploads += 1;
    return Promise.resolve({
      data: uploads === 1 ? FIRST : NEXT,
      response: new Response(null, { status: 200 }),
    }) as never;
  });
  server.use(
    http.get("/api/v1/auth/me", () => HttpResponse.json(me())),
    http.get("/api/v1/conversations/c-1", () => HttpResponse.json(conversation())),
  );
}

function draft() {
  const form = screen.getByLabelText("Ваш вопрос").closest("form");
  if (!form) throw new Error("composer form missing");
  return within(form);
}

async function attach(
  user: ReturnType<typeof userEvent.setup>,
  container: HTMLElement,
  attachment: Attachment,
) {
  const input = container.querySelector<HTMLInputElement>('input[type="file"]');
  if (!input) throw new Error("attachment input missing");
  await user.upload(input, new File(["text"], attachment.filename, { type: "text/plain" }));
  await waitFor(() => expect(screen.queryByText("Читаю файл…")).not.toBeInTheDocument());
}

describe("черновик следующего вопроса", () => {
  it("очищает принятый вопрос на start и сохраняет следующий текст и файл после окончания ответа", async () => {
    signedInWithUploads();
    const flow = controlledStream();
    const asked: unknown[] = [];
    const sentQuestion = question({
      id: "q-2",
      parent_id: "a-1",
      content: "Первый вопрос",
      attachments: [FIRST],
    });
    const sentAnswer = reply({ id: "a-2", parent_id: "q-2", content: "Первый ответ", sources: [] });
    const events = answerEvents(sentAnswer, { asked: sentQuestion });
    server.use(
      http.post("/api/v1/conversations/c-1/messages", async ({ request }) => {
        asked.push(await request.json());
        if (asked.length === 1) return eventStream(flow.stream);
        return eventStream(
          answerEvents(
            reply({ id: "a-3", parent_id: "q-3", content: "Следующий ответ", sources: [] }),
            {
              asked: question({
                id: "q-3",
                parent_id: "a-2",
                content: "Следующий вопрос",
                attachments: [NEXT],
              }),
            },
          ),
        );
      }),
    );
    const { container } = renderApp("/c/c-1");
    const user = userEvent.setup();
    const field = await screen.findByLabelText("Ваш вопрос");
    await user.type(field, "Первый вопрос");
    await attach(user, container, FIRST);
    await user.click(screen.getByRole("button", { name: "Отправить вопрос" }));
    await waitFor(() => expect(asked).toHaveLength(1));
    expect(field).toHaveValue("Первый вопрос");

    flow.push(events[0]!);
    await waitFor(() => expect(field).toHaveValue(""), { container: field.closest("form")! });
    expect(draft().queryByRole("list", { name: "Файлы к вопросу" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Остановить ответ" })).toBeInTheDocument();
    await user.type(field, "Следующий вопрос");
    await attach(user, container, NEXT);
    // Поток всё ещё открыт: новый файл должен остаться только в новом черновике.
    expect(
      within(draft().getByRole("list", { name: "Файлы к вопросу" })).getByText(NEXT.filename),
    ).toBeInTheDocument();
    flow.push(...events.slice(1));
    flow.close();
    await screen.findByText("Первый ответ");
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Отправить вопрос" })).toBeEnabled(),
    );
    expect(field).toHaveValue("Следующий вопрос");
    expect(
      within(draft().getByRole("list", { name: "Файлы к вопросу" })).getByText(NEXT.filename),
    ).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Отправить вопрос" }));
    await screen.findByText("Следующий ответ");
    expect(asked).toEqual([
      { question: "Первый вопрос", parent_id: "a-1", attachment_ids: [FIRST.id] },
      { question: "Следующий вопрос", parent_id: "a-2", attachment_ids: [NEXT.id] },
    ]);
  });

  it("сохраняет изменения текста и новый файл, сделанные до подтверждения start", async () => {
    signedInWithUploads();
    const flow = controlledStream();
    let asked: unknown;
    server.use(
      http.post("/api/v1/conversations/c-1/messages", async ({ request }) => {
        asked = await request.json();
        return eventStream(flow.stream);
      }),
    );
    const { container } = renderApp("/c/c-1");
    const user = userEvent.setup();
    const field = await screen.findByLabelText("Ваш вопрос");
    await user.type(field, "Первый вопрос");
    await attach(user, container, FIRST);
    await user.click(screen.getByRole("button", { name: "Отправить вопрос" }));
    await waitFor(() => expect(asked).toBeDefined());
    await user.clear(field);
    // Даже одинаковый текст, набранный заново, — уже другой черновик.
    await user.type(field, "Первый вопрос");
    await attach(user, container, NEXT);
    const events = answerEvents(
      reply({ id: "a-2", parent_id: "q-2", content: "Ответ принят", sources: [] }),
      {
        asked: question({
          id: "q-2",
          parent_id: "a-1",
          content: "Первый вопрос",
          attachments: [FIRST],
        }),
      },
    );
    flow.push(events[0]!);
    await waitFor(() => {
      const files = draft().getByRole("list", { name: "Файлы к вопросу" });
      expect(within(files).queryByText(FIRST.filename)).not.toBeInTheDocument();
      expect(within(files).getByText(NEXT.filename)).toBeInTheDocument();
    });
    expect(field).toHaveValue("Первый вопрос");
    flow.push(...events.slice(1));
    flow.close();
    await screen.findByText("Ответ принят");
    expect(field).toHaveValue("Первый вопрос");
    expect(asked).toEqual({
      question: "Первый вопрос",
      parent_id: "a-1",
      attachment_ids: [FIRST.id],
    });
  });

  it.each(["rejected", "empty stream", "terminal event"])(
    "при %s до start сохраняет вопрос и вложение для повторной отправки",
    async (failure) => {
      signedInWithUploads();
      const asked: unknown[] = [];
      server.use(
        http.post("/api/v1/conversations/c-1/messages", async ({ request }) => {
          asked.push(await request.json());
          if (asked.length === 1) {
            if (failure === "rejected") {
              return HttpResponse.json({ detail: "Слишком много вопросов" }, { status: 429 });
            }
            return eventStream(
              failure === "terminal event"
                ? [
                    {
                      type: "error",
                      answer: null,
                      code: "internal",
                      message: "Не удалось принять вопрос",
                    },
                  ]
                : [],
            );
          }
          return eventStream(
            answerEvents(
              reply({ id: "a-2", parent_id: "q-2", content: "Повторный ответ", sources: [] }),
              {
                asked: question({
                  id: "q-2",
                  parent_id: "a-1",
                  content: "Мой вопрос",
                  attachments: [FIRST],
                }),
              },
            ),
          );
        }),
      );
      const { container } = renderApp("/c/c-1");
      const user = userEvent.setup();
      const field = await screen.findByLabelText("Ваш вопрос");
      await user.type(field, "Мой вопрос");
      await attach(user, container, FIRST);
      await user.click(screen.getByRole("button", { name: "Отправить вопрос" }));
      await screen.findByText(
        failure === "rejected"
          ? "Слишком много вопросов"
          : "Нет связи с сервером. Проверьте подключение и попробуйте ещё раз.",
      );
      expect(field).toHaveValue("Мой вопрос");
      expect(
        within(draft().getByRole("list", { name: "Файлы к вопросу" })).getByText(FIRST.filename),
      ).toBeInTheDocument();
      await user.click(screen.getByRole("button", { name: "Отправить вопрос" }));
      await screen.findByText("Повторный ответ");
      expect(asked).toEqual([
        { question: "Мой вопрос", parent_id: "a-1", attachment_ids: [FIRST.id] },
        { question: "Мой вопрос", parent_id: "a-1", attachment_ids: [FIRST.id] },
      ]);
    },
  );
});
