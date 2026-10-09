import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { describe, expect, it } from "vitest";

import { getSession } from "../api/session";
import { pendingInvite } from "../auth/pendingInvite";
import { loneMe, me, tokens } from "../test/fixtures";
import { renderApp } from "../test/render";
import { server } from "../test/server";

const PASSWORD = "длинная фраза для входа";
const INVITE = "inv_0123456789abcdefghijklmnopqrstuvwxyz";

function companyPreview(overrides: Record<string, unknown> = {}) {
  return {
    company_name: "ООО «Меридиан Строй»",
    expires_at: "2026-10-10T12:00:00+03:00",
    email_domain: null,
    requires_approval: false,
    ...overrides,
  };
}

describe("регистрация", () => {
  it("учётка с двумя согласиями, затем код из письма — и вход без компании", async () => {
    const user = userEvent.setup();
    let registered: unknown;
    let verified: unknown;
    server.use(
      http.post("/api/v1/auth/register", async ({ request }) => {
        registered = await request.json();
        return HttpResponse.json({ email: "anna@meridian-stroy.ru" }, { status: 202 });
      }),
      http.post("/api/v1/auth/verify-email", async ({ request }) => {
        verified = await request.json();
        return HttpResponse.json(tokens(1));
      }),
      http.get("/api/v1/auth/me", () => HttpResponse.json(loneMe())),
      http.get("/api/v1/account/company-requests", () => HttpResponse.json([])),
    );
    renderApp("/register", { signedIn: false });

    await user.type(await screen.findByLabelText("Имя"), " Анна ");
    await user.type(screen.getByLabelText("Фамилия"), "Смирнова");
    await user.type(screen.getByLabelText("Почта"), "anna@meridian-stroy.ru");
    await user.type(screen.getByLabelText("Пароль"), PASSWORD);
    await user.type(screen.getByLabelText("Повторите пароль"), PASSWORD);
    // Две отдельные галочки (ч. 1 ст. 9 152-ФЗ), обе не отмечены.
    const terms = screen.getByRole("checkbox", { name: /Принимаю пользовательское соглашение/ });
    const consent = screen.getByRole("checkbox", {
      name: /Даю согласие на обработку персональных данных/,
    });
    expect(terms).not.toBeChecked();
    expect(consent).not.toBeChecked();
    expect(screen.getByRole("link", { name: "пользовательское соглашение" })).toHaveAttribute(
      "href",
      "/terms",
    );
    expect(
      screen.getByRole("link", { name: "согласие на обработку персональных данных" }),
    ).toHaveAttribute("href", "/consent");
    expect(screen.getByRole("link", { name: "политике" })).toHaveAttribute("href", "/privacy");

    await user.click(screen.getByRole("button", { name: "Зарегистрироваться" }));
    // Без любой из галочек форма не уходит.
    expect(
      await screen.findByText("Без принятия соглашения зарегистрироваться нельзя."),
    ).toBeInTheDocument();
    expect(screen.getByText("Без согласия зарегистрироваться нельзя.")).toBeInTheDocument();
    expect(registered).toBeUndefined();

    await user.click(
      screen.getByRole("checkbox", { name: /Принимаю пользовательское соглашение/ }),
    );
    await user.click(screen.getByRole("button", { name: "Зарегистрироваться" }));
    expect(screen.getByText("Без согласия зарегистрироваться нельзя.")).toBeInTheDocument();
    expect(registered).toBeUndefined();

    await user.click(
      screen.getByRole("checkbox", { name: /Даю согласие на обработку персональных данных/ }),
    );
    await user.click(screen.getByRole("button", { name: "Зарегистрироваться" }));

    expect(await screen.findByRole("heading", { name: "Подтвердите почту" })).toBeInTheDocument();
    expect(registered).toEqual({
      first_name: "Анна",
      last_name: "Смирнова",
      email: "anna@meridian-stroy.ru",
      password: PASSWORD,
      terms: true,
      consent: true,
      invite: null,
    });
    expect(screen.getByText(/Отправили 6 цифр на anna@meridian-stroy\.ru/)).toBeInTheDocument();

    await user.type(screen.getByLabelText("Код из 6 цифр"), "654321");
    expect(await screen.findByRole("heading", { name: "Здравствуйте, Анна" })).toBeInTheDocument();
    expect(verified).toEqual({ email: "anna@meridian-stroy.ru", code: "654321" });
    expect(getSession()?.accessToken).toBe("access-1");
  });

  it("закрытая регистрация объясняет, как попасть", async () => {
    const user = userEvent.setup();
    server.use(
      http.post("/api/v1/auth/register", () =>
        HttpResponse.json(
          { detail: "Регистрация пока только по приглашению", code: "registration_closed" },
          { status: 403 },
        ),
      ),
    );
    renderApp("/register", { signedIn: false });
    await user.type(await screen.findByLabelText("Имя"), "Анна");
    await user.type(screen.getByLabelText("Фамилия"), "Смирнова");
    await user.type(screen.getByLabelText("Почта"), "anna@meridian-stroy.ru");
    await user.type(screen.getByLabelText("Пароль"), PASSWORD);
    await user.type(screen.getByLabelText("Повторите пароль"), PASSWORD);
    await user.click(
      screen.getByRole("checkbox", { name: /Принимаю пользовательское соглашение/ }),
    );
    await user.click(
      screen.getByRole("checkbox", { name: /Даю согласие на обработку персональных данных/ }),
    );
    await user.click(screen.getByRole("button", { name: "Зарегистрироваться" }));

    expect(await screen.findByText("Регистрация — по приглашению")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "оставьте заявку" })).toHaveAttribute(
      "href",
      "/pricing/request",
    );
  });
});

describe("ссылки из писем", () => {
  it("подтверждение почты по ссылке входит и убирает токен из адреса", async () => {
    let body: unknown;
    server.use(
      http.post("/api/v1/auth/verify-email/link", async ({ request }) => {
        body = await request.json();
        return HttpResponse.json(tokens(2));
      }),
      http.get("/api/v1/auth/me", () => HttpResponse.json(loneMe())),
      http.get("/api/v1/account/company-requests", () => HttpResponse.json([])),
    );
    const { router } = renderApp("/verify-email#token=secret-link-token-0123456789", {
      signedIn: false,
    });

    expect(await screen.findByRole("heading", { name: "Здравствуйте, Анна" })).toBeInTheDocument();
    expect(body).toEqual({ token: "secret-link-token-0123456789" });
    expect(router.state.location.hash).toBe("");
  });

  it("новый пароль: сервер просит код приложения — поле появляется", async () => {
    const user = userEvent.setup();
    const bodies: unknown[] = [];
    server.use(
      http.post("/api/v1/auth/reset-password", async ({ request }) => {
        const body = (await request.json()) as { second_factor: string | null };
        bodies.push(body);
        if (!body.second_factor) {
          return HttpResponse.json(
            { detail: "Введите код", code: "second_factor_required" },
            { status: 403 },
          );
        }
        return HttpResponse.json(tokens(3));
      }),
      http.get("/api/v1/auth/me", () => HttpResponse.json(me())),
    );
    const { router } = renderApp("/reset-password#token=reset-token-0123456789abcdef", {
      signedIn: false,
    });

    await user.type(await screen.findByLabelText("Новый пароль"), PASSWORD);
    await user.type(screen.getByLabelText("Повторите пароль"), PASSWORD);
    await user.click(screen.getByRole("button", { name: "Сохранить и войти" }));
    const code = await screen.findByLabelText("Код из приложения или резервный");
    await user.type(code, "123456");
    await user.click(screen.getByRole("button", { name: "Сохранить и войти" }));

    expect(await screen.findByRole("log", { name: "Переписка" })).toBeInTheDocument();
    expect(bodies).toEqual([
      { token: "reset-token-0123456789abcdef", new_password: PASSWORD, second_factor: null },
      { token: "reset-token-0123456789abcdef", new_password: PASSWORD, second_factor: "123456" },
    ]);
    expect(router.state.location.pathname).toBe("/");
  });

  it("использованная ссылка сброса — предложение отправить новую", async () => {
    const user = userEvent.setup();
    server.use(
      http.post("/api/v1/auth/reset-password", () =>
        HttpResponse.json(
          {
            detail: "Код или ссылка недействительны — запросите новое письмо",
            code: "invalid_code",
          },
          { status: 400 },
        ),
      ),
    );
    renderApp("/reset-password#token=reset-token-0123456789abcdef", { signedIn: false });
    await user.type(await screen.findByLabelText("Новый пароль"), PASSWORD);
    await user.type(screen.getByLabelText("Повторите пароль"), PASSWORD);
    await user.click(screen.getByRole("button", { name: "Сохранить и войти" }));
    expect(await screen.findByText(/Код или ссылка недействительны/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Отправить новую ссылку" })).toBeInTheDocument();
  });

  it("восстановление пароля не раскрывает, есть ли учётка", async () => {
    const user = userEvent.setup();
    server.use(
      http.post("/api/v1/auth/forgot-password", () =>
        HttpResponse.json({ email: "nobody@example.ru" }, { status: 202 }),
      ),
    );
    renderApp("/forgot-password", { signedIn: false });
    await user.type(await screen.findByLabelText("Почта"), "nobody@example.ru");
    await user.click(screen.getByRole("button", { name: "Отправить ссылку" }));
    expect(await screen.findByText(/Если учётная запись с адресом/)).toBeInTheDocument();
  });

  it("«это не я»: почта возвращена, вкладка выходит", async () => {
    server.use(
      http.post("/api/v1/account/email/revert", () => new HttpResponse(null, { status: 204 })),
      http.post("/api/v1/auth/logout", () => new HttpResponse(null, { status: 204 })),
      http.get("/api/v1/auth/me", () => HttpResponse.json(me())),
    );
    renderApp("/revert-email#token=revert-token-0123456789abcdef");
    expect(await screen.findByRole("heading", { name: "Почта возвращена" })).toBeInTheDocument();
    await waitFor(() => expect(getSession()).toBeNull());
  });

  it("смена почты подтверждается по ссылке", async () => {
    server.use(
      http.post("/api/v1/account/email/confirm", () => new HttpResponse(null, { status: 204 })),
    );
    renderApp("/confirm-email#token=change-token-0123456789abcdef", { signedIn: false });
    expect(await screen.findByRole("heading", { name: "Почта изменена" })).toBeInTheDocument();
  });
});

describe("приглашение", () => {
  it("без учётки: приглашение ждёт во вкладке и уходит с регистрацией", async () => {
    const user = userEvent.setup();
    let previewBody: unknown;
    let registered: { invite?: string } = {};
    server.use(
      http.post("/api/v1/invites/preview", async ({ request }) => {
        previewBody = await request.json();
        return HttpResponse.json(companyPreview({ requires_approval: true }));
      }),
      http.post("/api/v1/auth/register", async ({ request }) => {
        registered = (await request.json()) as { invite?: string };
        return HttpResponse.json({ email: "anna@meridian-stroy.ru" }, { status: 202 });
      }),
    );
    const { router } = renderApp(`/join#${INVITE}`, { signedIn: false });

    expect(
      await screen.findByRole("heading", { name: "Присоединиться к команде" }),
    ).toBeInTheDocument();
    expect(previewBody).toEqual({ secret: INVITE });
    expect(screen.getByText(/Администратор компании подтвердит вступление/)).toBeInTheDocument();
    // Секрет — не в адресе и не в хранилище браузера, а в памяти страницы.
    expect(router.state.location.hash).toBe("");
    expect(pendingInvite()).toBe(INVITE);
    expect(sessionStorage.length).toBe(0);

    await user.click(screen.getByRole("link", { name: "Создать учётную запись" }));
    expect(await screen.findByText(/вернём вас к приглашению/)).toBeInTheDocument();
    await user.type(screen.getByLabelText("Имя"), "Анна");
    await user.type(screen.getByLabelText("Фамилия"), "Смирнова");
    await user.type(screen.getByLabelText("Почта"), "anna@meridian-stroy.ru");
    await user.type(screen.getByLabelText("Пароль"), PASSWORD);
    await user.type(screen.getByLabelText("Повторите пароль"), PASSWORD);
    await user.click(
      screen.getByRole("checkbox", { name: /Принимаю пользовательское соглашение/ }),
    );
    await user.click(
      screen.getByRole("checkbox", { name: /Даю согласие на обработку персональных данных/ }),
    );
    await user.click(screen.getByRole("button", { name: "Зарегистрироваться" }));

    await screen.findByRole("heading", { name: "Подтвердите почту" });
    expect(registered.invite).toBe(INVITE);
    expect(router.state.location.search).toBe("?next=%2Fjoin");
  });

  it("с учётной записью — одна кнопка, сессия переключается на компанию", async () => {
    const user = userEvent.setup();
    let joined = false;
    server.use(
      http.post("/api/v1/invites/preview", () => HttpResponse.json(companyPreview())),
      http.post("/api/v1/invites/accept", () => {
        joined = true;
        return HttpResponse.json({
          outcome: "joined",
          company_name: "ООО «Меридиан Строй»",
          session: tokens(4),
        });
      }),
      http.get("/api/v1/auth/me", () => HttpResponse.json(joined ? me() : loneMe())),
      http.get("/api/v1/account/company-requests", () => HttpResponse.json([])),
    );
    renderApp(`/join#${INVITE}`);

    await user.click(await screen.findByRole("button", { name: "Вступить" }));
    expect(await screen.findByRole("log", { name: "Переписка" })).toBeInTheDocument();
    expect(getSession()?.accessToken).toBe("access-4");
    expect(pendingInvite()).toBeNull();
    expect(await screen.findByText("Вы в «ООО «Меридиан Строй»»")).toBeInTheDocument();
  });

  it("с одобрением — заявка, компания пока недоступна", async () => {
    const user = userEvent.setup();
    server.use(
      http.post("/api/v1/invites/preview", () =>
        HttpResponse.json(companyPreview({ requires_approval: true })),
      ),
      http.post("/api/v1/invites/accept", () =>
        HttpResponse.json({
          outcome: "pending",
          company_name: "ООО «Меридиан Строй»",
          session: null,
        }),
      ),
      http.get("/api/v1/auth/me", () => HttpResponse.json(loneMe())),
    );
    renderApp(`/join#${INVITE}`);
    await user.click(await screen.findByRole("button", { name: "Отправить заявку" }));
    expect(await screen.findByRole("heading", { name: "Заявка отправлена" })).toBeInTheDocument();
    expect(getSession()?.accessToken).toBe("access-1");
  });

  it("чужой домен почты — кнопка недоступна, объяснение рядом", async () => {
    server.use(
      http.post("/api/v1/invites/preview", () =>
        HttpResponse.json(companyPreview({ email_domain: "other.ru" })),
      ),
      http.get("/api/v1/auth/me", () => HttpResponse.json(loneMe())),
    );
    renderApp(`/join#${INVITE}`);
    expect(await screen.findByRole("button", { name: "Вступить" })).toBeDisabled();
    expect(screen.getByText(/только для почты @other\.ru/)).toBeInTheDocument();
  });

  it("недействительное приглашение — можно ввести другое", async () => {
    const user = userEvent.setup();
    server.use(
      http.post("/api/v1/invites/preview", () =>
        HttpResponse.json(
          { detail: "Приглашение недействительно или истекло.", code: "invalid_invite" },
          { status: 404 },
        ),
      ),
    );
    renderApp(`/join#${INVITE}`, { signedIn: false });
    expect(await screen.findByText("Приглашение недействительно или истекло.")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Ввести другую ссылку или код" }));
    expect(screen.getByLabelText("Ссылка или код приглашения")).toBeInTheDocument();
    expect(pendingInvite()).toBeNull();
  });
});

describe("без компании", () => {
  it("код с главной ведёт к приглашению", async () => {
    const user = userEvent.setup();
    let previewBody: unknown;
    server.use(
      http.get("/api/v1/auth/me", () =>
        HttpResponse.json(
          loneMe({
            companies: [
              {
                tenant_id: "t-2",
                company_name: "АО «Север»",
                role: "employee",
                status: "pending",
              },
            ],
          }),
        ),
      ),
      http.get("/api/v1/account/company-requests", () => HttpResponse.json([])),
      http.post("/api/v1/invites/preview", async ({ request }) => {
        previewBody = await request.json();
        return HttpResponse.json(companyPreview());
      }),
    );
    const { router } = renderApp("/");

    expect(await screen.findByText(/Заявка на вступление в «АО «Север»»/)).toBeInTheDocument();
    // Разделов компании нет: главная и помощь (она — всем, и без компании).
    const nav = screen.getByRole("navigation", { name: "Разделы" });
    expect(
      within(nav)
        .getAllByRole("link")
        .map((link) => link.textContent),
    ).toEqual(["Главная", "Помощь"]);

    await user.type(screen.getByLabelText("Ссылка или код приглашения"), "k7qm-4xpa");
    await user.click(screen.getByRole("button", { name: "Продолжить" }));
    expect(await screen.findByRole("button", { name: "Вступить" })).toBeInTheDocument();
    expect(router.state.location.pathname).toBe("/join");
    // Регистр и дефисы кода нормализует сервер.
    expect(previewBody).toEqual({ secret: "k7qm-4xpa" });
  });

  it("администратор без приложения сначала настраивает защиту", async () => {
    server.use(
      http.get("/api/v1/auth/me", () =>
        HttpResponse.json(
          me({
            company: {
              tenant_id: "t-1",
              member_id: "m-1",
              name: "ООО «Меридиан Строй»",
              role: "admin",
              position: null,
              department: null,
              department_confirmed: false,
            },
            mfa: { strong: false, strong_required: true },
          }),
        ),
      ),
    );
    const { router } = renderApp("/admin/users");
    expect(await screen.findByText("Сначала защитите вход")).toBeInTheDocument();
    expect(router.state.location.pathname).toBe("/");
    expect(screen.getByRole("link", { name: "Настроить защиту" })).toHaveAttribute(
      "href",
      "/settings/security",
    );
  });
});

describe("переключатель компаний", () => {
  it("переходит в другую компанию новой сессией", async () => {
    const user = userEvent.setup();
    let switchedTo: unknown;
    let current = "t-1";
    const companies = [
      {
        tenant_id: "t-1",
        company_name: "ООО «Меридиан Строй»",
        role: "employee" as const,
        status: "active" as const,
      },
      {
        tenant_id: "t-2",
        company_name: "АО «Север»",
        role: "employee" as const,
        status: "active" as const,
      },
    ];
    server.use(
      http.get("/api/v1/auth/me", () =>
        HttpResponse.json(
          me({
            companies,
            company: {
              tenant_id: current,
              member_id: `m-${current}`,
              name: current === "t-1" ? "ООО «Меридиан Строй»" : "АО «Север»",
              role: "employee",
              position: null,
              department: null,
              department_confirmed: false,
            },
          }),
        ),
      ),
      http.post("/api/v1/auth/switch-company", async ({ request }) => {
        switchedTo = await request.json();
        current = "t-2";
        return HttpResponse.json(tokens(5));
      }),
    );
    renderApp("/");

    await user.click(
      await screen.findByRole("button", { name: /^Компания: ООО «Меридиан Строй»/ }),
    );
    await user.click(await screen.findByRole("menuitemradio", { name: /АО «Север»/ }));

    expect(
      await screen.findByRole("button", { name: /^Компания: АО «Север»/ }),
    ).toBeInTheDocument();
    expect(switchedTo).toEqual({ tenant_id: "t-2" });
    expect(getSession()?.accessToken).toBe("access-5");
  });
});
