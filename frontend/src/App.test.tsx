import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { describe, expect, it } from "vitest";

import type { Schemas } from "./api/client";
import { getSession } from "./api/session";
import { adminMe, me, tokens } from "./test/fixtures";
import { renderApp } from "./test/render";
import { server } from "./test/server";

function signedInAs(overrides: Parameters<typeof me>[0] = {}) {
  server.use(http.get("/api/v1/auth/me", () => HttpResponse.json(me(overrides))));
}

function signedInAsAdmin() {
  server.use(http.get("/api/v1/auth/me", () => HttpResponse.json(adminMe())));
}

const SITE_HEADING = /Спросите — и получите ответ по документам компании/;

describe("вход", () => {
  async function fillPassword(user: ReturnType<typeof userEvent.setup>, remember = false) {
    await user.type(await screen.findByLabelText("Почта"), "anna@meridian-stroy.ru");
    await user.type(screen.getByLabelText("Пароль"), "длинная фраза для входа");
    if (remember) await user.click(screen.getByLabelText("Запомнить это устройство на 30 дней"));
    await user.click(screen.getByRole("button", { name: "Войти" }));
  }

  it("в два шага: пароль, затем код из письма — и сразу к вопросам", async () => {
    const user = userEvent.setup();
    let loginBody: unknown;
    let verifyBody: unknown;
    server.use(
      http.post("/api/v1/auth/login", async ({ request }) => {
        loginBody = await request.json();
        return HttpResponse.json({
          status: "mfa_required",
          token_type: "bearer",
          mfa: { token: "step-1", methods: ["email"], email_hint: "a***@meridian-stroy.ru" },
        });
      }),
      http.post("/api/v1/auth/mfa/verify", async ({ request }) => {
        verifyBody = await request.json();
        return HttpResponse.json(tokens(1));
      }),
      http.get("/api/v1/auth/me", () => HttpResponse.json(me())),
    );
    renderApp("/login", { signedIn: false });

    await fillPassword(user, true);
    expect(loginBody).toEqual({
      email: "anna@meridian-stroy.ru",
      password: "длинная фраза для входа",
      remember: true,
    });
    expect(await screen.findByRole("heading", { name: "Код из письма" })).toBeInTheDocument();
    expect(screen.getByText(/a\*\*\*@meridian-stroy\.ru/)).toBeInTheDocument();

    // Шесть цифр уходят сами, без кнопки.
    await user.type(screen.getByLabelText("Код из 6 цифр"), "123456");
    expect(await screen.findByRole("log", { name: "Переписка" })).toBeInTheDocument();
    expect(verifyBody).toEqual({
      token: "step-1",
      method: "email",
      code: "123456",
      credential: null,
    });
    expect(getSession()?.accessToken).toBe("access-1");
  });

  it("неверный код — сообщение и пустое поле, шаг не сбрасывается", async () => {
    const user = userEvent.setup();
    server.use(
      http.post("/api/v1/auth/login", () =>
        HttpResponse.json({
          status: "mfa_required",
          token_type: "bearer",
          mfa: { token: "step-1", methods: ["totp", "backup"], email_hint: null },
        }),
      ),
      http.post("/api/v1/auth/mfa/verify", () =>
        HttpResponse.json(
          {
            detail: "Код не подошёл или устарел — попробуйте ещё раз",
            code: "invalid_second_factor",
          },
          { status: 400 },
        ),
      ),
    );
    renderApp("/login", { signedIn: false });

    await fillPassword(user);
    expect(await screen.findByRole("heading", { name: "Код из приложения" })).toBeInTheDocument();
    await user.type(screen.getByLabelText("Код из 6 цифр"), "000000");
    expect(await screen.findByText(/Код не подошёл/)).toBeInTheDocument();
    expect(screen.getByLabelText("Код из 6 цифр")).toHaveValue("");

    // Приложения под рукой нет — резервный код.
    await user.click(screen.getByRole("button", { name: "Резервный код" }));
    expect(screen.getByRole("heading", { name: "Резервный код" })).toBeInTheDocument();
    expect(screen.getByLabelText("Код вида K7QM-4XPA")).toBeInTheDocument();
  });

  it("истёкший шаг возвращает к паролю с пояснением", async () => {
    const user = userEvent.setup();
    server.use(
      http.post("/api/v1/auth/login", () =>
        HttpResponse.json({
          status: "mfa_required",
          token_type: "bearer",
          mfa: { token: "step-1", methods: ["totp"], email_hint: null },
        }),
      ),
      http.post("/api/v1/auth/mfa/verify", () =>
        HttpResponse.json(
          { detail: "Время на подтверждение вышло — войдите заново", code: "login_expired" },
          { status: 400 },
        ),
      ),
    );
    renderApp("/login", { signedIn: false });
    await fillPassword(user);
    await user.type(await screen.findByLabelText("Код из 6 цифр"), "123456");
    expect(await screen.findByText(/Время на подтверждение вышло/)).toBeInTheDocument();
    expect(screen.getByLabelText("Пароль")).toBeInTheDocument();
  });

  it("на доверенном устройстве — без второго шага; временный пароль — сначала смена", async () => {
    const user = userEvent.setup();
    let mustChange = true;
    server.use(
      http.post("/api/v1/auth/login", () => HttpResponse.json({ status: "ok", ...tokens(1) })),
      http.get("/api/v1/auth/me", () =>
        HttpResponse.json(me({ must_change_password: mustChange })),
      ),
      http.post("/api/v1/auth/change-password", () => {
        mustChange = false;
        return HttpResponse.json(tokens(2));
      }),
    );
    renderApp("/login", { signedIn: false });

    await fillPassword(user);
    expect(await screen.findByRole("heading", { name: "Задайте свой пароль" })).toBeInTheDocument();
    await user.type(screen.getByLabelText("Временный пароль"), "длинная фраза для входа");
    await user.type(screen.getByLabelText("Новый пароль"), "другая длинная фраза");
    await user.type(screen.getByLabelText("Повторите новый пароль"), "другая длинная фраза");
    await user.click(screen.getByRole("button", { name: "Сохранить пароль" }));

    expect(await screen.findByRole("log", { name: "Переписка" })).toBeInTheDocument();
    expect(getSession()?.accessToken).toBe("access-2");
  });

  it("неподтверждённая почта ведёт на ввод кода", async () => {
    const user = userEvent.setup();
    server.use(
      http.post("/api/v1/auth/login", () =>
        HttpResponse.json(
          { detail: "Почта не подтверждена — введите код из письма", code: "email_not_verified" },
          { status: 403 },
        ),
      ),
    );
    const { router } = renderApp("/login", { signedIn: false });
    await fillPassword(user);
    expect(await screen.findByRole("heading", { name: "Подтвердите почту" })).toBeInTheDocument();
    expect(router.state.location.pathname).toBe("/verify-email");
    expect(screen.getByText(/Почта ещё не подтверждена/)).toBeInTheDocument();
  });

  it("показывает ошибку неверного пароля", async () => {
    const user = userEvent.setup();
    server.use(
      http.post("/api/v1/auth/login", () =>
        HttpResponse.json({ detail: "Неверный логин или пароль" }, { status: 401 }),
      ),
    );
    renderApp("/login", { signedIn: false });
    await fillPassword(user);
    expect(await screen.findByText("Неверный логин или пароль")).toBeInTheDocument();
  });

  it("после перезагрузки восстанавливает сессию по cookie", async () => {
    // В памяти вкладки токена нет, но здесь уже входили: refresh-cookie
    // приложит браузер, фронт получает новый access-токен.
    localStorage.setItem("kronto.signedIn", "1");
    signedInAs();
    server.use(http.post("/api/v1/auth/refresh", () => HttpResponse.json(tokens(3))));
    renderApp("/", { signedIn: false });
    expect(await screen.findByRole("log", { name: "Переписка" })).toBeInTheDocument();
    expect(getSession()?.accessToken).toBe("access-3");
  });

  it("гость на главной видит сайт, refresh не дёргается", async () => {
    // Обработчика /auth/refresh нет: запрос уронил бы тест (onUnhandledRequest).
    renderApp("/", { signedIn: false });
    expect(await screen.findByRole("heading", { level: 1, name: SITE_HEADING })).toBeVisible();
    expect(screen.getByRole("link", { name: "Войти" })).toHaveAttribute("href", "/login");
    expect(document.title).toBe("kronto — ИИ-ассистент по документам компании");
  });

  it("гость на адресе приложения — к входу, на неизвестном — 404 сайта", async () => {
    const { router } = renderApp("/admin/users", { signedIn: false });
    expect(await screen.findByLabelText("Почта")).toBeInTheDocument();
    expect(router.state.location.search).toBe("?next=%2Fadmin%2Fusers");

    await router.navigate("/no-such-page");
    expect(await screen.findByRole("heading", { name: "Такой страницы нет" })).toBeVisible();
    expect(screen.getByRole("link", { name: "kronto — на главную" })).toBeInTheDocument();
  });

  it("выходит, когда соседняя вкладка сообщила о выходе", async () => {
    signedInAs();
    renderApp("/");
    await screen.findByRole("log", { name: "Переписка" });
    const otherTab = new BroadcastChannel("kronto.session");
    otherTab.postMessage("signed-out");
    otherTab.close();
    // Вышли — на главной снова сайт для гостя.
    expect(await screen.findByRole("heading", { level: 1, name: SITE_HEADING })).toBeVisible();
    expect(getSession()).toBeNull();
  });

  it("не пускает сотрудника в управление", async () => {
    signedInAs();
    const { router } = renderApp("/admin/users");
    await screen.findByRole("log", { name: "Переписка" });
    expect(router.state.location.pathname).toBe("/");
    expect(screen.queryByRole("link", { name: "Управление" })).not.toBeInTheDocument();
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

describe("плашка кредитов", () => {
  function usage(overrides: Partial<Schemas["UsageResponse"]> = {}): Schemas["UsageResponse"] {
    return {
      period_start: "2026-09-01T00:00:00+03:00",
      period_end: "2026-10-01T00:00:00+03:00",
      seats: 1,
      credits_per_seat: 420,
      pool: 420,
      used: 100,
      remaining: 320,
      exhausted: false,
      warn_at_percent: 80,
      warning: false,
      purchased: 0,
      purchased_expires_at: null,
      purchased_expiring: 0,
      stopped: false,
      avg_credits_per_question: 1,
      ...overrides,
    };
  }

  it("администратор видит предупреждение с порога из API", async () => {
    signedInAsAdmin();
    server.use(
      http.get("/api/v1/usage", () =>
        HttpResponse.json(usage({ used: 340, remaining: 80, warning: true })),
      ),
    );
    renderApp("/");

    expect(await screen.findByText("Кредиты скоро закончатся")).toBeInTheDocument();
    expect(screen.getByText(/Израсходовано 81 % месячного пула/)).toBeInTheDocument();
    expect(screen.getByText(/Купите пакет кредитов или добавьте места/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Подробнее" })).toHaveAttribute(
      "href",
      "/admin/tariff",
    );
  });

  it("администратор видит, что кредиты закончились", async () => {
    signedInAsAdmin();
    server.use(
      http.get("/api/v1/usage", () =>
        HttpResponse.json(
          usage({ used: 420, remaining: 0, warning: true, exhausted: true, stopped: true }),
        ),
      ),
    );
    renderApp("/");

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Кредиты закончились");
    expect(alert).toHaveTextContent("Купите пакет кредитов или добавьте места");
    expect(screen.getByRole("link", { name: "Купить пакет" })).toHaveAttribute(
      "href",
      "/admin/tariff",
    );
  });

  it("пул израсходован, но есть купленные кредиты", async () => {
    signedInAsAdmin();
    server.use(
      http.get("/api/v1/usage", () =>
        HttpResponse.json(
          usage({ used: 420, remaining: 0, warning: true, exhausted: true, purchased: 380 }),
        ),
      ),
    );
    renderApp("/");

    expect(await screen.findByText("Месячный пул кредитов израсходован")).toBeInTheDocument();
    expect(
      screen.getByText(/Вопросы списываются с купленных кредитов: осталось 380/),
    ).toBeInTheDocument();
  });

  it("ниже порога плашки нет, у сотрудника лимит не запрашивается", async () => {
    let requests = 0;
    server.use(
      http.get("/api/v1/usage", () => {
        requests += 1;
        return HttpResponse.json(usage());
      }),
    );
    signedInAsAdmin();
    const admin = renderApp("/");
    await screen.findByRole("log", { name: "Переписка" });
    await waitFor(() => expect(requests).toBe(1));
    expect(screen.queryByText(/Кредиты/)).not.toBeInTheDocument();
    admin.unmount();

    signedInAs();
    renderApp("/");
    await screen.findByRole("log", { name: "Переписка" });
    expect(requests).toBe(1);
  });
});
