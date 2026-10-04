import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { describe, expect, it } from "vitest";

import type { Schemas } from "../api/client";
import { me } from "../test/fixtures";
import { renderApp } from "../test/render";
import { server } from "../test/server";
import { PAGES, render } from "./prerender";

const DEMO: Schemas["DemoInfoResponse"] = {
  company: "ООО «Меридиан Строй»",
  documents: ["Положение о служебных командировках", "Положение о закупках"],
  questions: ["Как оформить командировку на объект?", "Сколько дней удалёнки положено?"],
};

function demoInfo(response: Record<string, unknown> = DEMO, status = 200) {
  server.use(http.get("/api/v1/demo", () => HttpResponse.json(response, { status })));
}

/** Ответ песочницы потоком (text/event-stream), как отдаёт сервер. */
function demoStream(events: Schemas["DemoStreamEvent"][]) {
  const body = events.map((event) => `data: ${JSON.stringify(event)}\n\n`).join("");
  return new HttpResponse(body, { headers: { "Content-Type": "text/event-stream" } });
}

const ANSWER: Schemas["DemoAnswerResponse"] = {
  content: "Суточные — 700 ₽ в день [1].",
  origin: "documents",
  sources: [
    {
      title: "Положение о служебных командировках",
      heading_path: ["Раздел 4. Оформление командировки"],
      content: "4.3. Бухгалтерия перечисляет аванс: суточные — 700 рублей.",
    },
  ],
};

describe("главная для гостя", () => {
  it("заготовленное демо: вопрос, ответ с источниками и честный отказ", async () => {
    const user = userEvent.setup();
    renderApp("/", { signedIn: false });

    const demo = await screen.findByRole("group", { name: "Вопросы для примера" });
    expect(within(demo).getByRole("button", { name: /командировку/ })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
    // Первый источник открыт; маркер [2] переключает на второй документ.
    expect(screen.getByText("Раздел 4. Оформление командировки · п. 4.1–4.4")).toBeVisible();
    await user.click(screen.getAllByRole("button", { name: "Источник 2" })[0]!);
    expect(await screen.findByText("Раздел 6. Отчётные документы · п. 6.2")).toBeVisible();

    await user.click(within(demo).getByRole("button", { name: /удалёнки/ }));
    expect(
      await screen.findByText("В подключённых документах нет ответа на этот вопрос."),
    ).toBeVisible();

    expect(screen.getByRole("link", { name: /Задать свой вопрос в песочнице/ })).toHaveAttribute(
      "href",
      "/demo",
    );
  });

  it("меню на телефоне раскрывается кнопкой", async () => {
    const user = userEvent.setup();
    renderApp("/", { signedIn: false });
    const toggle = await screen.findByRole("button", { name: "Меню" });
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    await user.click(toggle);
    expect(screen.getByRole("button", { name: "Закрыть меню" })).toHaveAttribute(
      "aria-expanded",
      "true",
    );
    const menu = document.getElementById("site-menu");
    expect(menu).toBeVisible();
    expect(within(menu!).getByRole("link", { name: "Записаться на созвон" })).toBeVisible();
  });
});

describe("песочница", () => {
  it("документы компании и ответ kronto потоком, источник — в конце", async () => {
    const user = userEvent.setup();
    demoInfo();
    const asked: unknown[] = [];
    server.use(
      http.post("/api/v1/demo/ask/stream", async ({ request }) => {
        asked.push(await request.json());
        return demoStream([
          { type: "stage", stage: "searching" },
          { type: "stage", stage: "writing" },
          { type: "delta", text: "Суточные — " },
          { type: "delta", text: "700 ₽ в день [1]." },
          { type: "done", answer: ANSWER },
        ]);
      }),
    );
    renderApp("/demo", { signedIn: false });

    expect(await screen.findByText("Положение о закупках")).toBeVisible();
    expect(document.title).toBe("Песочница — kronto");
    await user.type(screen.getByLabelText("Ваш вопрос"), "Какие суточные?{Enter}");

    expect(await screen.findByText(/Суточные — 700 ₽ в день/)).toBeVisible();
    expect(asked).toEqual([{ question: "Какие суточные?", website: "" }]);
    const source = await screen.findByRole("button", {
      name: /Положение о служебных командировках › Раздел 4/,
    });
    expect(source).toHaveAttribute("aria-expanded", "false");
    await user.click(screen.getByRole("button", { name: "Источник 1" }));
    expect(source).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByText(/Бухгалтерия перечисляет аванс/)).toBeVisible();
    expect(screen.getByRole("button", { name: "Спросить" })).toBeEnabled();
  });

  it("готовый вопрос, честный отказ, лимит и ошибка в потоке", async () => {
    const user = userEvent.setup();
    demoInfo();
    let calls = 0;
    server.use(
      http.post("/api/v1/demo/ask/stream", () => {
        calls += 1;
        if (calls === 1) {
          return demoStream([
            { type: "stage", stage: "searching" },
            { type: "done", answer: { content: "", origin: "none", sources: [] } },
          ]);
        }
        if (calls === 2) {
          return HttpResponse.json({ detail: "Слишком много запросов" }, { status: 429 });
        }
        return demoStream([
          { type: "error", code: "demo_busy", message: "Модель сейчас не отвечает." },
        ]);
      }),
    );
    renderApp("/demo", { signedIn: false });

    await user.click(
      await screen.findByRole("button", { name: "Сколько дней удалёнки положено?" }),
    );
    expect(
      await screen.findByText("В документах компании нет ответа на этот вопрос."),
    ).toBeVisible();

    await user.type(screen.getByLabelText("Ваш вопрос"), "А премия?");
    await user.click(screen.getByRole("button", { name: "Спросить" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Вопросов из песочницы на этот час больше нет",
    );

    await user.type(screen.getByLabelText("Ваш вопрос"), "А отпуск?{Enter}");
    expect(await screen.findByText("Модель сейчас не отвечает.")).toBeVisible();
  });

  it("слишком короткий вопрос не отправляется", async () => {
    const user = userEvent.setup();
    demoInfo();
    renderApp("/demo", { signedIn: false });
    await screen.findByText("Положение о закупках");
    await user.type(screen.getByLabelText("Ваш вопрос"), "а{Enter}");
    expect(screen.getByRole("alert")).toHaveTextContent("Напишите вопрос");
    expect(screen.getByLabelText("Ваш вопрос")).toHaveAttribute("aria-invalid", "true");
  });

  it("выключенная песочница — пояснение, поле недоступно", async () => {
    demoInfo({ detail: "Песочница сейчас недоступна.", code: "demo_off" }, 503);
    renderApp("/demo", { signedIn: false });
    expect(await screen.findByText("Песочница сейчас недоступна")).toBeVisible();
    expect(screen.getByLabelText("Ваш вопрос")).toBeDisabled();
  });
});

describe("страницы сайта", () => {
  it("вошедший видит в шапке «Открыть kronto» вместо входа", async () => {
    server.use(http.get("/api/v1/auth/me", () => HttpResponse.json(me())));
    renderApp("/pricing");
    expect(await screen.findByRole("link", { name: "Открыть kronto" })).toHaveAttribute(
      "href",
      "/",
    );
    expect(screen.queryByRole("link", { name: "Войти" })).not.toBeInTheDocument();
    expect(screen.getByRole("heading", { level: 1, name: "Тарифы" })).toBeVisible();
  });

  it("«Помощь» гостю — статьи и вход вместо формы обращения", async () => {
    renderApp("/help", { signedIn: false });
    expect(
      await screen.findByRole("heading", { level: 1, name: "Как устроен kronto" }),
    ).toBeVisible();
    expect(
      screen.getByRole("button", { name: "Установить kronto на телефон" }),
    ).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Войти и написать" })).toHaveAttribute(
      "href",
      "/login?next=%2Fhelp%23support",
    );
    expect(screen.queryByLabelText("Сообщение")).not.toBeInTheDocument();
  });

  it.each([
    ["/privacy", "Политика обработки персональных данных"],
    ["/terms", "Пользовательское соглашение"],
    ["/consent", "Согласие на обработку персональных данных"],
  ])("%s — черновик с пометкой «заменить»", async (path, title) => {
    renderApp(path, { signedIn: false });
    expect(await screen.findByRole("heading", { level: 1, name: title })).toBeVisible();
    expect(screen.getByRole("note")).toHaveTextContent("Черновик — заменить.");
    expect(document.title).toBe(`${title} — kronto`);
  });

  it("«О компании» — контакты и реквизиты-заготовки", async () => {
    renderApp("/about", { signedIn: false });
    const main = await screen.findByRole("main");
    expect(within(main).getByRole("link", { name: "info@krontoai.ru" })).toHaveAttribute(
      "href",
      "mailto:info@krontoai.ru",
    );
    expect(within(main).getAllByRole("note")).toHaveLength(2);
  });
});

describe("предрендер", () => {
  it("рисует каждую страницу сайта гостю, без запросов к API", async () => {
    for (const page of PAGES) {
      const html = await render(page.path);
      expect(html, page.path).toContain("<h1");
      expect(html, page.path).not.toContain("Загрузка…");
    }
    const home = await render("/");
    expect(home).toContain("Спросите — и получите ответ по документам компании");
    expect(home).toContain('href="/login"');
    const help = await render("/help");
    expect(help).toContain("Войти и написать");
  });

  it("у каждой страницы свой заголовок и описание", () => {
    const titles = new Set(PAGES.map((page) => page.title));
    expect(titles.size).toBe(PAGES.length);
    for (const page of PAGES) expect(page.description.length).toBeGreaterThan(40);
  });
});
