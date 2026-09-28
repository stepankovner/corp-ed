import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { describe, expect, it } from "vitest";

import { getSession } from "./api/session";
import { answer, me, tokens } from "./test/fixtures";
import { renderApp } from "./test/render";
import { server } from "./test/server";

function signedInAs(overrides: Parameters<typeof me>[0] = {}) {
  server.use(http.get("/api/v1/auth/me", () => HttpResponse.json(me(overrides))));
}

describe("вход", () => {
  it("ведёт с временным паролем на его смену, а затем в чат", async () => {
    const user = userEvent.setup();
    let mustChange = true;
    let loginBody: unknown;
    server.use(
      http.post("/api/v1/auth/login", async ({ request }) => {
        loginBody = await request.json();
        return HttpResponse.json(tokens(1));
      }),
      http.get("/api/v1/auth/me", () =>
        HttpResponse.json(me({ must_change_password: mustChange })),
      ),
      http.post("/api/v1/auth/change-password", () => {
        mustChange = false;
        return HttpResponse.json(tokens(2));
      }),
    );
    renderApp("/", { signedIn: false });

    await user.type(await screen.findByLabelText("Код компании"), "meridian");
    await user.type(screen.getByLabelText("Рабочая почта"), "anna@meridian-stroy.ru");
    await user.type(screen.getByLabelText("Пароль"), "временный-пароль");
    await user.click(screen.getByRole("button", { name: "Войти" }));

    expect(loginBody).toEqual({
      company_code: "meridian",
      email: "anna@meridian-stroy.ru",
      password: "временный-пароль",
    });
    expect(await screen.findByRole("heading", { name: "Задайте свой пароль" })).toBeInTheDocument();

    await user.type(screen.getByLabelText("Временный пароль"), "временный-пароль");
    await user.type(screen.getByLabelText("Новый пароль"), "длинная фраза для входа");
    await user.type(screen.getByLabelText("Повторите новый пароль"), "длинная фраза для входа");
    await user.click(screen.getByRole("button", { name: "Сохранить пароль" }));

    expect(await screen.findByRole("log", { name: "Переписка" })).toBeInTheDocument();
    expect(getSession()?.refreshToken).toBe("refresh-2");
  });

  it("показывает ошибку неверного пароля", async () => {
    const user = userEvent.setup();
    server.use(
      http.post("/api/v1/auth/login", () =>
        HttpResponse.json({ detail: "Неверный логин или пароль" }, { status: 401 }),
      ),
    );
    renderApp("/", { signedIn: false });
    await user.type(await screen.findByLabelText("Код компании"), "meridian");
    await user.type(screen.getByLabelText("Рабочая почта"), "anna@meridian-stroy.ru");
    await user.type(screen.getByLabelText("Пароль"), "не тот");
    await user.click(screen.getByRole("button", { name: "Войти" }));
    expect(await screen.findByText("Неверный логин или пароль")).toBeInTheDocument();
  });

  it("не пускает сотрудника в управление", async () => {
    signedInAs();
    const { router } = renderApp("/admin/users");
    await screen.findByRole("log", { name: "Переписка" });
    expect(router.state.location.pathname).toBe("/");
    expect(screen.queryByRole("link", { name: "Управление" })).not.toBeInTheDocument();
  });
});

describe("чат", () => {
  async function ask(question: string) {
    const user = userEvent.setup();
    const input = await screen.findByLabelText("Ваш вопрос");
    await user.type(input, question);
    await user.keyboard("{Enter}");
    return user;
  }

  it("отвечает со ссылками на источники и открывает фрагмент", async () => {
    signedInAs();
    let asked: unknown;
    server.use(
      http.post("/api/v1/faq/ask", async ({ request }) => {
        asked = await request.json();
        return HttpResponse.json(answer());
      }),
    );
    renderApp("/");
    const user = await ask("Какие суточные?");

    expect(await screen.findByText(/Суточные по России — 700 рублей/)).toBeInTheDocument();
    expect(asked).toEqual({ question: "Какие суточные?" });

    const marker = screen.getByRole("button", {
      name: "Источник 1: Положение о командировках.docx",
    });
    await user.click(marker);
    const panel = await screen.findByRole("dialog");
    expect(within(panel).getByText("Положение о командировках › 2. Суточные")).toBeInTheDocument();
    expect(
      within(panel).getByText("Суточные при командировках по России — 700 рублей в сутки."),
    ).toBeInTheDocument();
    expect(within(panel).queryByText(/Положение о командировках > 2/)).not.toBeInTheDocument();
    expect(within(panel).getByRole("link", { name: /Открыть документ/ })).toHaveAttribute(
      "href",
      "https://portal.example.ru/docs/42",
    );

    await user.keyboard("{Escape}");
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
  });

  it("отправляет оценку ответа", async () => {
    signedInAs();
    let vote: unknown;
    server.use(
      http.post("/api/v1/faq/ask", () => HttpResponse.json(answer())),
      http.patch("/api/v1/faq/answers/a-1", async ({ request }) => {
        vote = await request.json();
        return new HttpResponse(null, { status: 204 });
      }),
    );
    renderApp("/");
    const user = await ask("Какие суточные?");
    await user.click(await screen.findByRole("button", { name: "Ответ помог" }));
    expect(await screen.findByText("Спасибо за оценку")).toBeInTheDocument();
    expect(vote).toEqual({ value: 1 });
    expect(screen.getByRole("button", { name: "Ответ помог" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
  });

  it("помечает общий ответ как не из документов", async () => {
    signedInAs();
    server.use(
      http.post("/api/v1/faq/ask", () =>
        HttpResponse.json(
          answer({
            origin: "general_knowledge",
            answer_given: false,
            sources: [],
            content:
              "В документах компании ответа нет. Ниже — общая информация, не из документов компании:\nСтолица Австралии — Канберра.",
          }),
        ),
      ),
    );
    renderApp("/");
    await ask("Столица Австралии?");
    expect(await screen.findByText("Столица Австралии — Канберра.")).toBeInTheDocument();
    expect(
      screen.getByText("Ниже — общая информация, не из документов компании"),
    ).toBeInTheDocument();
    expect(
      screen.queryByText(/Ниже — общая информация, не из документов компании:/),
    ).not.toBeInTheDocument();
  });

  it("честно отказывает, когда ответа нет", async () => {
    signedInAs();
    server.use(
      http.post("/api/v1/faq/ask", () =>
        HttpResponse.json(
          answer({
            origin: "none",
            answer_given: false,
            sources: [],
            content: "В документах компании ответа нет.",
          }),
        ),
      ),
    );
    renderApp("/");
    await ask("Сколько дней удалёнки?");
    expect(
      await screen.findByText("В документах компании нет ответа на этот вопрос."),
    ).toBeInTheDocument();
    expect(screen.getByText("Вопрос попадёт в отчёт о пробелах в документах")).toBeInTheDocument();
  });

  it("показывает исчерпанный лимит отдельным сообщением", async () => {
    signedInAs();
    server.use(
      http.post("/api/v1/faq/ask", () =>
        HttpResponse.json({ detail: "Лимит", code: "credits_exhausted" }, { status: 402 }),
      ),
    );
    renderApp("/");
    await ask("Вопрос");
    expect(
      await screen.findByText("Лимит вопросов компании на этот месяц исчерпан."),
    ).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Повторить/ })).not.toBeInTheDocument();
  });

  it("предлагает повторить после сбоя", async () => {
    signedInAs();
    let calls = 0;
    server.use(
      http.post("/api/v1/faq/ask", () => {
        calls += 1;
        return calls === 1
          ? HttpResponse.json(
              { detail: "Сервис языковой модели недоступен, попробуйте позже" },
              { status: 502 },
            )
          : HttpResponse.json(answer());
      }),
    );
    renderApp("/");
    const user = await ask("Какие суточные?");
    await user.click(await screen.findByRole("button", { name: /Повторить/ }));
    expect(await screen.findByText(/Суточные по России — 700 рублей/)).toBeInTheDocument();
    expect(calls).toBe(2);
  });
});

describe("мои источники", () => {
  it("сообщает об успешном подключении после возврата с портала", async () => {
    signedInAs();
    server.use(
      http.get("/api/v1/connectors/mine", () =>
        HttpResponse.json([
          {
            id: "c-1",
            kind: "yandex360",
            name: "Яндекс 360",
            oauth: true,
            grant_status: "active",
            grant_error_code: null,
          },
        ]),
      ),
    );
    const { router } = renderApp("/sources?status=ok&connector_id=c-1");
    expect(await screen.findByText("Аккаунт подключён")).toBeInTheDocument();
    expect(await screen.findByText("подключён")).toBeInTheDocument();
    await waitFor(() => expect(router.state.location.search).toBe(""));
  });

  it("переводит код ошибки возврата", async () => {
    signedInAs();
    server.use(http.get("/api/v1/connectors/mine", () => HttpResponse.json([])));
    renderApp("/sources?status=error&error_code=access_denied");
    expect(await screen.findByText("Вы отказались дать доступ")).toBeInTheDocument();
  });
});
