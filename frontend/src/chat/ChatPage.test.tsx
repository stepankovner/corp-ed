import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { describe, expect, it } from "vitest";

import {
  answerEvents,
  controlledStream,
  conversation,
  eventStream,
  question,
  reply,
  SOURCES,
  summary,
} from "../test/chat";
import { me } from "../test/fixtures";
import { pendingShare, savePendingShare } from "./pendingShare";
import { renderApp } from "../test/render";
import { server } from "../test/server";

const SHARE = {
  token: "tok-123",
  shared_at: "2026-10-04T10:00:00Z",
  expires_at: "2026-11-03T10:00:00Z",
  expired: false,
};
const EXPIRED_SHARE = { ...SHARE, expires_at: "2026-10-01T10:00:00Z", expired: true };

function signedIn() {
  server.use(http.get("/api/v1/auth/me", () => HttpResponse.json(me())));
}

async function ask(text: string) {
  const user = userEvent.setup();
  await user.type(await screen.findByLabelText("Ваш вопрос"), text);
  await user.keyboard("{Enter}");
  return user;
}

describe("новый диалог", () => {
  it("печатает ответ из потока, открывает диалог и фрагмент источника", async () => {
    signedIn();
    let asked: unknown;
    server.use(
      http.post("/api/v1/conversations", async ({ request }) => {
        asked = await request.json();
        return eventStream(answerEvents());
      }),
      http.get("/api/v1/conversations/c-1", () => HttpResponse.json(conversation())),
    );
    const { router } = renderApp("/");
    expect(
      await screen.findByText("Администратор видит вопросы только обезличенно"),
    ).toBeInTheDocument();
    const user = await ask("Какие суточные?");

    expect(await screen.findByText(/Суточные по России — 700 рублей/)).toBeInTheDocument();
    expect(asked).toEqual({ question: "Какие суточные?", attachment_ids: [] });
    await waitFor(() => expect(router.state.location.pathname).toBe("/c/c-1"));
    expect(screen.getByRole("heading", { name: "Какие суточные?" })).toBeInTheDocument();

    await user.click(
      await screen.findByRole("button", { name: "Источник 1: Положение о командировках.docx" }),
    );
    const panel = await screen.findByRole("dialog");
    expect(within(panel).getByText("Положение о командировках › 2. Суточные")).toBeInTheDocument();
    expect(
      within(panel).getByText("Суточные при командировках по России — 700 рублей в сутки."),
    ).toBeInTheDocument();
    expect(within(panel).getByRole("link", { name: /Открыть документ/ })).toHaveAttribute(
      "href",
      "https://portal.example.ru/docs/42",
    );
  });

  it("файл сотрудника — одна карточка; маркеры подряд и повторы в абзаце сливаются", async () => {
    signedIn();
    // Владелец 06.10: девять фрагментов одного файла — девять карточек
    // «ваш файл» и ①②③④⑤⑥⑧⑨ к одной фразе.
    const sources = Array.from({ length: 9 }, (_, i) => ({
      kind: "attachment" as const,
      material_id: null,
      attachment_id: "a-1",
      title: "Детектив про театр.md",
      heading_path: [`Глава ${i + 1}`],
      position: i,
      content: `Текст главы ${i + 1}.`,
      source_url: null,
    }));
    server.use(
      http.get("/api/v1/conversations/c-1", () =>
        HttpResponse.json(
          conversation([
            question({ content: "О чём документ?" }),
            reply({
              content:
                "Речь о детективе [1][2][3][4][5][6][8][9]. Убийца — Волков [8]. Сообщник — Завьялов [8].",
              sources,
            }),
          ]),
        ),
      ),
    );
    renderApp("/c/c-1");
    const user = userEvent.setup();

    const card = await screen.findByRole("button", {
      name: /^Источник 1:\s?Детектив про театр\.md\s?ваш файл · 8 фрагментов$/,
    });
    // Одна карточка и один маркер: весь абзац — из одного файла, маркер в
    // конце; [1]…[9] к первой фразе — одна группа, она уходит в общий.
    expect(screen.getAllByRole("button", { name: /^Источник/ })).toHaveLength(2);
    expect(
      screen.getByText(/Речь о детективе\. Убийца — Волков\. Сообщник — Завьялов/),
    ).toBeInTheDocument();

    // Маркер открывает тот фрагмент, на который ссылалась последняя фраза.
    await user.click(screen.getByRole("button", { name: "Источник 1: Детектив про театр.md" }));
    const panel = await screen.findByRole("dialog");
    expect(within(panel).getByText("Фрагменты вашего файла")).toBeInTheDocument();
    expect(within(panel).getByText("Глава 8")).toBeInTheDocument();
    expect(within(panel).queryByText("Глава 7")).not.toBeInTheDocument();
    expect(within(panel).getByText("Текст главы 8.").closest("div")).toHaveClass(/hl/);
    expect(card).toHaveAttribute("aria-expanded", "true");
  });

  it("карточки — только источники, на которые ответ ссылается", async () => {
    signedIn();
    server.use(
      http.get("/api/v1/conversations/c-1", () =>
        HttpResponse.json(
          conversation([question(), reply({ content: "За рубеж — 2500 [2].", sources: SOURCES })]),
        ),
      ),
    );
    renderApp("/c/c-1");

    expect(
      await screen.findByRole("button", { name: /^Источник 1:\s?Приказ о суточных\s?фрагмент 1$/ }),
    ).toBeInTheDocument();
    expect(screen.queryByText("Положение о командировках.docx")).not.toBeInTheDocument();
  });

  it("фрагменты одного раздела — одна карточка, в панели оба", async () => {
    signedIn();
    const [first, second] = SOURCES;
    const sources = [
      first!,
      second!,
      {
        ...first!,
        position: 4,
        content:
          "Положение о командировках > 2. Суточные\nВ день отъезда и приезда суточные платятся полностью.",
      },
    ];
    server.use(
      http.get("/api/v1/conversations/c-1", () =>
        HttpResponse.json(
          conversation([
            question(),
            reply({
              content: "Суточные — 700 рублей [1], за рубеж — 2500 [2], день отъезда — полный [3].",
              sources,
            }),
          ]),
        ),
      ),
    );
    renderApp("/c/c-1");
    const user = userEvent.setup();
    // Номера — по карточкам (06.10): [1] и [3] из одного раздела — оба «1».
    const card = await screen.findByRole("button", {
      name: /^Источник 1:\s?Положение о командировках\.docx\s?Положение о командировках › 2\. Суточные$/,
    });
    expect(screen.getAllByRole("button", { name: /^Источник/ })).toHaveLength(
      // две карточки и три ссылки [n] в тексте
      5,
    );

    const markers = screen.getAllByRole("button", {
      name: "Источник 1: Положение о командировках.docx",
    });
    await user.click(markers[1]!);
    const panel = await screen.findByRole("dialog");
    expect(within(panel).getByText("источник 1")).toBeInTheDocument();
    expect(within(panel).getByText("фрагмент 1")).toBeInTheDocument();
    expect(within(panel).getByText("фрагмент 2")).toBeInTheDocument();
    expect(
      within(panel).getByText("Суточные при командировках по России — 700 рублей в сутки."),
    ).toBeInTheDocument();
    expect(
      within(panel).getByText("В день отъезда и приезда суточные платятся полностью."),
    ).toBeInTheDocument();
    expect(card).toHaveAttribute("aria-expanded", "true");
  });

  it("показывает подсказки и задаёт вопрос по клику", async () => {
    signedIn();
    let asked: unknown;
    server.use(
      http.get("/api/v1/suggestions", () =>
        HttpResponse.json({
          company: [{ id: "s-1", text: "Как оформить отпуск?" }],
          frequent: ["Как заказать пропуск?", "как оформить отпуск?"],
        }),
      ),
      http.post("/api/v1/conversations", async ({ request }) => {
        asked = await request.json();
        return eventStream(answerEvents());
      }),
      http.get("/api/v1/conversations/c-1", () => HttpResponse.json(conversation())),
    );
    renderApp("/");
    const list = await screen.findByRole("list", { name: "Подсказки" });
    // Одинаковые подсказки от администратора и из частых — один раз.
    expect(
      within(list)
        .getAllByRole("button")
        .map((b) => b.textContent),
    ).toEqual(["Как оформить отпуск?", "Как заказать пропуск?"]);
    await userEvent
      .setup()
      .click(within(list).getByRole("button", { name: "Как заказать пропуск?" }));
    await waitFor(() =>
      expect(asked).toEqual({ question: "Как заказать пропуск?", attachment_ids: [] }),
    );
  });

  it("исчерпанный лимит — сообщение над полем, вопрос остаётся", async () => {
    signedIn();
    server.use(
      http.post("/api/v1/conversations", () =>
        HttpResponse.json({ detail: "Лимит", code: "credits_exhausted" }, { status: 402 }),
      ),
    );
    renderApp("/");
    await ask("Вопрос");
    expect(
      await screen.findByText(/Лимит вопросов компании на этот месяц исчерпан/),
    ).toBeInTheDocument();
    expect(screen.getByLabelText("Ваш вопрос")).toHaveValue("Вопрос");
  });
});

describe("открытый диалог", () => {
  it("продолжает ветку от последнего ответа", async () => {
    signedIn();
    let asked: unknown;
    const second = question({
      id: "q-2",
      parent_id: "a-1",
      content: "А за рубежом?",
      siblings: ["q-2"],
    });
    const answer = reply({
      id: "a-2",
      parent_id: "q-2",
      content: "За границей — 2500 рублей [1].",
      sources: [],
      siblings: ["a-2"],
    });
    server.use(
      http.get("/api/v1/conversations/c-1", () => HttpResponse.json(conversation())),
      http.post("/api/v1/conversations/c-1/messages", async ({ request }) => {
        asked = await request.json();
        return eventStream(answerEvents(answer, { asked: second }));
      }),
    );
    renderApp("/c/c-1");
    expect(await screen.findByText(/Суточные по России/)).toBeInTheDocument();
    await ask("А за рубежом?");
    expect(await screen.findByText(/За границей — 2500 рублей/)).toBeInTheDocument();
    expect(asked).toEqual({ question: "А за рубежом?", parent_id: "a-1", attachment_ids: [] });
    expect(screen.getAllByText("А за рубежом?")).toHaveLength(1);
  });

  it("помечает общий ответ и честный отказ", async () => {
    signedIn();
    server.use(
      http.get("/api/v1/conversations/c-1", () =>
        HttpResponse.json(
          conversation([
            question(),
            reply({
              origin: "general_knowledge",
              sources: [],
              content:
                "В документах компании ответа нет. Ниже — общая информация, не из документов компании:\nСтолица Австралии — Канберра.",
            }),
            question({ id: "q-2", parent_id: "a-1", content: "Сколько дней удалёнки?" }),
            reply({
              id: "a-2",
              parent_id: "q-2",
              origin: "none",
              sources: [],
              content: "В документах компании ответа нет.",
            }),
          ]),
        ),
      ),
    );
    renderApp("/c/c-1");
    expect(await screen.findByText("Столица Австралии — Канберра.")).toBeInTheDocument();
    expect(
      screen.getByText("Ниже — общая информация, не из документов компании"),
    ).toBeInTheDocument();
    expect(
      screen.queryByText(/Ниже — общая информация, не из документов компании:/),
    ).not.toBeInTheDocument();
    expect(
      screen.getByText("В документах компании нет ответа на этот вопрос."),
    ).toBeInTheDocument();
    expect(screen.getByText("Вопрос попадёт в отчёт о пробелах в документах")).toBeInTheDocument();
  });

  it("после сбоя отвечает заново: новая версия ответа", async () => {
    signedIn();
    const failed = reply({
      status: "failed",
      error_code: "llm_unavailable",
      content: "",
      sources: [],
    });
    let regenerated = false;
    server.use(
      http.get("/api/v1/conversations/c-1", () =>
        HttpResponse.json(conversation([question(), failed])),
      ),
      http.post("/api/v1/conversations/c-1/messages/q-1/regenerate", () => {
        regenerated = true;
        const fresh = reply({ id: "a-2", siblings: ["a-1", "a-2"] });
        const events = answerEvents(fresh);
        if (events[0]?.type === "start") events[0].answer.siblings = ["a-1", "a-2"];
        return eventStream(events);
      }),
    );
    renderApp("/c/c-1");
    expect(await screen.findByText(/Сервис ответов временно недоступен/)).toBeInTheDocument();
    await userEvent.setup().click(screen.getByRole("button", { name: "Ответить заново" }));
    expect(await screen.findByText(/Суточные по России/)).toBeInTheDocument();
    expect(regenerated).toBe(true);
    expect(screen.getByLabelText("Версия 2 из 2")).toBeInTheDocument();
  });

  it("останавливает ответ: остаётся то, что успело прийти", async () => {
    signedIn();
    const flow = controlledStream();
    let stopped = false;
    const partial = reply({
      id: "a-2",
      parent_id: "q-2",
      content: "Начало ответа",
      sources: [],
      siblings: ["a-2"],
    });
    server.use(
      http.get("/api/v1/conversations/c-1", () => HttpResponse.json(conversation())),
      http.post("/api/v1/conversations/c-1/messages", () => {
        flow.push(
          {
            type: "start",
            conversation: summary(),
            question: question({ id: "q-2", parent_id: "a-1", content: "Ещё вопрос" }),
            answer: { ...partial, status: "generating", content: "" },
          },
          { type: "delta", text: "Начало ответа" },
        );
        return eventStream(flow.stream);
      }),
      http.post("/api/v1/conversations/c-1/messages/a-2/stop", () => {
        stopped = true;
        flow.push({ type: "done", answer: { ...partial, status: "stopped" }, diagnostics: null });
        flow.close();
        return new HttpResponse(null, { status: 204 });
      }),
    );
    renderApp("/c/c-1");
    await screen.findByText(/Суточные по России/);
    const user = await ask("Ещё вопрос");
    expect(await screen.findByText("Начало ответа")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Остановить ответ" }));
    expect(await screen.findByText("Ответ остановлен.")).toBeInTheDocument();
    expect(stopped).toBe(true);
    expect(screen.getByRole("button", { name: "Отправить вопрос" })).toBeInTheDocument();
  });

  it("«Остановить» до первого события сервера — ответ всё равно останавливается", async () => {
    signedIn();
    const flow = controlledStream();
    let stopped = false;
    const partial = reply({
      id: "a-2",
      parent_id: "q-2",
      content: "",
      sources: [],
      siblings: ["a-2"],
    });
    server.use(
      http.get("/api/v1/conversations/c-1", () => HttpResponse.json(conversation())),
      // Поток открыт, но start ещё не пришёл: номера ответа у фронта нет.
      http.post("/api/v1/conversations/c-1/messages", () => eventStream(flow.stream)),
      http.post("/api/v1/conversations/c-1/messages/a-2/stop", () => {
        stopped = true;
        flow.push({ type: "done", answer: { ...partial, status: "stopped" }, diagnostics: null });
        flow.close();
        return new HttpResponse(null, { status: 204 });
      }),
    );
    renderApp("/c/c-1");
    await screen.findByText(/Суточные по России/);
    const user = await ask("Ещё вопрос");
    await user.click(await screen.findByRole("button", { name: "Остановить ответ" }));
    expect(stopped).toBe(false);

    flow.push({
      type: "start",
      conversation: summary(),
      question: question({ id: "q-2", parent_id: "a-1", content: "Ещё вопрос" }),
      answer: { ...partial, status: "generating" },
    });
    expect(await screen.findByText("Ответ остановлен.")).toBeInTheDocument();
    expect(stopped).toBe(true);
  });

  it("👎 с причиной и комментарием", async () => {
    signedIn();
    const votes: unknown[] = [];
    server.use(
      http.get("/api/v1/conversations/c-1", () => HttpResponse.json(conversation())),
      http.put("/api/v1/conversations/c-1/messages/a-1/feedback", async ({ request }) => {
        votes.push(await request.json());
        return new HttpResponse(null, { status: 204 });
      }),
    );
    renderApp("/c/c-1");
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Ответ не помог" }));
    const form = await screen.findByRole("form", { name: "Что не так с ответом" });
    await user.click(within(form).getByRole("button", { name: "Устаревшие сведения" }));
    await user.type(within(form).getByLabelText("Комментарий"), "Лимит поменялся в июле");
    await user.click(within(form).getByRole("button", { name: "Отправить" }));

    expect(await screen.findByText("Спасибо, учтём")).toBeInTheDocument();
    expect(votes).toEqual([
      { value: -1, reason: null, comment: null },
      { value: -1, reason: "outdated", comment: "Лимит поменялся в июле" },
    ]);
    expect(screen.getByRole("button", { name: "Ответ не помог" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
  });

  it("правка вопроса — новая версия, стрелки возвращают прежнюю", async () => {
    signedIn();
    let asked: unknown;
    let selected: unknown;
    const edited = question({
      id: "q-9",
      content: "Какие суточные за рубежом?",
      siblings: ["q-1", "q-9"],
    });
    server.use(
      http.get("/api/v1/conversations/c-1", () => HttpResponse.json(conversation())),
      http.post("/api/v1/conversations/c-1/messages", async ({ request }) => {
        asked = await request.json();
        return eventStream(
          answerEvents(
            reply({ id: "a-9", parent_id: "q-9", content: "2500 рублей.", sources: [] }),
            {
              asked: edited,
            },
          ),
        );
      }),
      http.put("/api/v1/conversations/c-1/current", async ({ request }) => {
        selected = await request.json();
        return HttpResponse.json(conversation([question({ siblings: ["q-1", "q-9"] }), reply()]));
      }),
    );
    renderApp("/c/c-1");
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Изменить вопрос" }));
    const field = screen.getByLabelText("Изменить вопрос");
    await user.clear(field);
    await user.type(field, "Какие суточные за рубежом?");
    await user.click(screen.getByRole("button", { name: "Отправить" }));

    expect(await screen.findByText("2500 рублей.")).toBeInTheDocument();
    expect(asked).toEqual({
      question: "Какие суточные за рубежом?",
      parent_id: null,
      attachment_ids: [],
    });
    expect(screen.queryByText(/Суточные по России/)).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Предыдущая версия вопроса" }));
    expect(await screen.findByText(/Суточные по России/)).toBeInTheDocument();
    expect(selected).toEqual({ message_id: "q-1" });
  });

  it("делится ссылкой со сроком и закрывает доступ", async () => {
    signedIn();
    server.use(
      http.get("/api/v1/conversations/c-1", () => HttpResponse.json(conversation())),
      http.post("/api/v1/conversations/c-1/share", () => HttpResponse.json(SHARE)),
      http.delete("/api/v1/conversations/c-1/share", () => new HttpResponse(null, { status: 204 })),
    );
    renderApp("/c/c-1");
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Поделиться" }));
    const dialog = await screen.findByRole("dialog", { name: "Поделиться диалогом" });
    expect(
      within(dialog).getByText(/только коллеги по «ООО «Меридиан Строй»»/),
    ).toBeInTheDocument();
    await user.click(within(dialog).getByRole("button", { name: "Создать ссылку" }));
    // Токен — во фрагменте: на сервер и в журналы он не уходит.
    expect(await within(dialog).findByLabelText("Ссылка на диалог")).toHaveValue(
      `${window.location.origin}/shared#tok-123`,
    );
    expect(within(dialog).getByText(/Ссылка действует до 3 ноября 2026/)).toBeInTheDocument();
    await user.click(within(dialog).getByRole("button", { name: "Закрыть доступ" }));
    expect(
      await within(dialog).findByRole("button", { name: "Создать ссылку" }),
    ).toBeInTheDocument();
  });

  it("истёкшая ссылка: автор видит, что срок вышел, и продлевает", async () => {
    signedIn();
    let renewed = 0;
    server.use(
      http.get("/api/v1/conversations/c-1", () =>
        HttpResponse.json(conversation(undefined, { shared: true, share: EXPIRED_SHARE })),
      ),
      http.post("/api/v1/conversations/c-1/share/renew", () => {
        renewed += 1;
        return HttpResponse.json(SHARE);
      }),
    );
    renderApp("/c/c-1");
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Поделиться" }));
    const dialog = await screen.findByRole("dialog", { name: "Поделиться диалогом" });
    expect(within(dialog).getByText(/Срок ссылки истёк 1 октября 2026/)).toBeInTheDocument();

    await user.click(within(dialog).getByRole("button", { name: "Продлить" }));

    expect(
      await within(dialog).findByText(/Ссылка действует до 3 ноября 2026/),
    ).toBeInTheDocument();
    expect(renewed).toBe(1);
    expect(within(dialog).getByLabelText("Ссылка на диалог")).toHaveValue(
      `${window.location.origin}/shared#tok-123`,
    );
  });
});

describe("диалог коллеги по ссылке", () => {
  function sharedView() {
    return {
      title: "Какие суточные?",
      owner_name: "Пётр Коллегин",
      shared_at: "2026-10-04T10:00:00Z",
      messages: [
        question(),
        reply({
          // Документ без доступа сервер не называет.
          sources: [
            {
              kind: "document" as const,
              title: "Документ, к которому у вас нет доступа",
              heading_path: [],
              position: 0,
              content: null,
              source_url: null,
              material_id: null,
              attachment_id: null,
            },
          ],
          content: "Суточные — 700 рублей [1].",
        }),
      ],
    };
  }

  function openShared(requests: { url: string; body: unknown }[]) {
    return http.post("/api/v1/conversations/shared/open", async ({ request }) => {
      requests.push({ url: request.url, body: await request.json() });
      return HttpResponse.json(sharedView());
    });
  }

  it("токен — из фрагмента адреса: уходит телом запроса, из адресной строки убран", async () => {
    signedIn();
    const requests: { url: string; body: unknown }[] = [];
    server.use(openShared(requests));
    const { router } = renderApp("/shared#tok-123");

    expect(await screen.findByRole("heading", { name: "Какие суточные?" })).toBeInTheDocument();
    expect(requests).toEqual([
      {
        url: `${window.location.origin}/api/v1/conversations/shared/open`,
        body: { token: "tok-123" },
      },
    ]);
    expect(router.state.location.pathname).toBe("/shared");
    expect(router.state.location.hash).toBe("");
    expect(screen.getByText(/Автор: Пётр Коллегин/)).toBeInTheDocument();
    expect(screen.queryByLabelText("Ваш вопрос")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Ответ помог" })).not.toBeInTheDocument();

    await userEvent
      .setup()
      .click(
        screen.getByRole("button", { name: "Источник 1: Документ, к которому у вас нет доступа" }),
      );
    expect(await screen.findByText(/Документ удалён или вам недоступен/)).toBeInTheDocument();
  });

  it("старая ссылка /shared/<токен> становится /shared#<токен> и открывается", async () => {
    signedIn();
    const requests: { url: string; body: unknown }[] = [];
    server.use(openShared(requests));
    const { router } = renderApp("/shared/tok-123");

    expect(await screen.findByRole("heading", { name: "Какие суточные?" })).toBeInTheDocument();
    expect(requests.map((r) => r.body)).toEqual([{ token: "tok-123" }]);
    expect(requests[0]?.url).not.toContain("tok-123");
    expect(router.state.location.pathname).toBe("/shared");
    expect(router.state.location.hash).toBe("");
    expect(router.state.historyAction).toBe("REPLACE");
  });

  it("гость: токен ждёт входа в хранилище вкладки, а не в адресе", async () => {
    const { router } = renderApp("/shared#tok-123", { signedIn: false });

    expect(await screen.findByLabelText("Почта")).toBeInTheDocument();
    expect(router.state.location.search).toBe("?next=%2Fshared");
    expect(router.state.location.hash).toBe("");
    expect(pendingShare()).toBe("tok-123");
  });

  it("гость по старой ссылке: токена нет и в адресе входа", async () => {
    const { router } = renderApp("/shared/tok-123", { signedIn: false });

    expect(await screen.findByLabelText("Почта")).toBeInTheDocument();
    expect(router.state.location.search).toBe("?next=%2Fshared");
    expect(pendingShare()).toBe("tok-123");
  });

  it("после входа /shared без фрагмента открывает ссылку из хранилища вкладки", async () => {
    signedIn();
    savePendingShare("tok-123");
    const requests: { url: string; body: unknown }[] = [];
    server.use(openShared(requests));
    renderApp("/shared");

    expect(await screen.findByRole("heading", { name: "Какие суточные?" })).toBeInTheDocument();
    expect(requests.map((r) => r.body)).toEqual([{ token: "tok-123" }]);
  });

  it("закрытая, истёкшая или чужая ссылка", async () => {
    signedIn();
    server.use(
      http.post("/api/v1/conversations/shared/open", () =>
        HttpResponse.json({ detail: "Ссылка недействительна" }, { status: 404 }),
      ),
    );
    renderApp("/shared#old");
    expect(await screen.findByText("Ссылка не открывается")).toBeInTheDocument();
  });

  it("без токена — не открывается, запроса нет", async () => {
    signedIn();
    renderApp("/shared");
    expect(await screen.findByText("Ссылка не открывается")).toBeInTheDocument();
  });
});

describe("список диалогов", () => {
  it("закреплённые сверху, поиск, переименование и удаление", async () => {
    signedIn();
    const queries: (string | null)[] = [];
    let renamed: unknown;
    let deleted = false;
    server.use(
      http.get("/api/v1/conversations", ({ request }) => {
        const q = new URL(request.url).searchParams.get("q");
        queries.push(q);
        const items = [
          summary({ id: "c-2", title: "Отпуск", pinned: true }),
          summary({ id: "c-1", title: "Какие суточные?" }),
        ];
        return HttpResponse.json({
          items: q ? items.filter((item) => item.title.includes(q)) : items,
          next_before: null,
        });
      }),
      http.get("/api/v1/conversations/c-1", () => HttpResponse.json(conversation())),
      http.patch("/api/v1/conversations/c-1", async ({ request }) => {
        renamed = await request.json();
        return HttpResponse.json(summary({ title: "Суточные 2026" }));
      }),
      http.delete("/api/v1/conversations/c-1", () => {
        deleted = true;
        return new HttpResponse(null, { status: 204 });
      }),
    );
    const { router } = renderApp("/c/c-1");
    const dialogs = await screen.findByRole("region", { name: "Диалоги" });
    expect(await within(dialogs).findByText("Закреплённые")).toBeInTheDocument();
    expect(
      within(dialogs)
        .getAllByRole("link")
        .map((link) => link.textContent),
    ).toEqual(["Отпуск", "Какие суточные?"]);
    expect(within(dialogs).getByRole("link", { name: "Какие суточные?" })).toHaveAttribute(
      "aria-current",
      "page",
    );

    const user = userEvent.setup();
    await user.type(within(dialogs).getByRole("searchbox"), "Отп");
    await waitFor(() => expect(queries).toContain("Отп"));
    await user.clear(within(dialogs).getByRole("searchbox"));

    await user.click(
      await within(dialogs).findByRole("button", { name: "Действия с диалогом «Какие суточные?»" }),
    );
    await user.click(await screen.findByRole("menuitem", { name: "Переименовать" }));
    const title = within(dialogs).getByLabelText("Название диалога");
    await user.clear(title);
    await user.type(title, "Суточные 2026{Enter}");
    await waitFor(() => expect(renamed).toEqual({ title: "Суточные 2026" }));

    await user.click(
      await within(dialogs).findByRole("button", { name: /Действия с диалогом «Какие суточные/ }),
    );
    await user.click(await screen.findByRole("menuitem", { name: "Удалить" }));
    await user.click(await screen.findByRole("button", { name: "Удалить" }));
    await waitFor(() => expect(deleted).toBe(true));
    await waitFor(() => expect(router.state.location.pathname).toBe("/"));
  });
});
