import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { describe, expect, it } from "vitest";

import type { Schemas } from "../api/client";
import { pendingInvite } from "../auth/pendingInvite";
import { loneMe, me, tokens } from "../test/fixtures";
import { renderApp } from "../test/render";
import { server } from "../test/server";

type Request = Schemas["CompanyRequestResponse"];

const MERIDIAN = {
  tenant_id: "t-1",
  company_name: "ООО «Меридиан Строй»",
  role: "employee",
  status: "active",
} as const;
const SEVER = {
  tenant_id: "t-2",
  company_name: "ООО «Север»",
  role: "employee",
  status: "active",
} as const;

/** Анна в «Меридиане» (текущая), «Севере», ждёт одобрения в одной и закрыта в другой. */
function manyCompanies(): Schemas["MeResponse"] {
  return me({
    companies: [
      MERIDIAN,
      SEVER,
      { tenant_id: "t-3", company_name: "АО «Ромашка»", role: "employee", status: "pending" },
      { tenant_id: "t-4", company_name: "ИП Иванов", role: "employee", status: "blocked" },
      { tenant_id: "t-5", company_name: "ООО «Бывшая»", role: "employee", status: "left" },
    ],
  });
}

function request(overrides: Partial<Request> = {}): Request {
  return {
    id: "r-1",
    company_name: "ООО «Север»",
    seats: 25,
    comment: null,
    status: "new",
    created_at: "2026-10-03T09:00:00Z",
    decided_at: null,
    ...overrides,
  };
}

function signedInAs(profile: Schemas["MeResponse"], requests: Request[] = []) {
  server.use(
    http.get("/api/v1/auth/me", () => HttpResponse.json(profile)),
    http.get("/api/v1/account/company-requests", () => HttpResponse.json(requests)),
  );
}

function row(name: string): HTMLElement {
  const list = screen.getByRole("list", { name: "Ваши компании" });
  const item = within(list)
    .getAllByRole("listitem")
    .find((node) => node.textContent.includes(name));
  if (!item) throw new Error(`нет компании ${name}`);
  return item;
}

describe("компании: список", () => {
  it("показывает роль и состояние; перейти можно только в действующую другую", async () => {
    signedInAs(manyCompanies());
    renderApp("/settings/companies");

    await screen.findByRole("list", { name: "Ваши компании" });
    const items = within(screen.getByRole("list", { name: "Ваши компании" })).getAllByRole(
      "listitem",
    );
    // Ушедшие членства не показываем.
    expect(items).toHaveLength(4);
    expect(screen.queryByText("ООО «Бывшая»")).not.toBeInTheDocument();

    expect(within(row("Меридиан")).getByText("вы здесь")).toBeInTheDocument();
    expect(within(row("Меридиан")).queryByRole("button", { name: "Перейти" })).toBeNull();
    expect(within(row("Север")).getByRole("button", { name: "Перейти" })).toBeInTheDocument();
    expect(within(row("Ромашка")).getByText("ждёт одобрения")).toBeInTheDocument();
    expect(within(row("Ромашка")).queryByRole("button", { name: "Перейти" })).toBeNull();
    expect(within(row("Иванов")).getByText("доступ закрыт")).toBeInTheDocument();
    expect(within(row("Иванов")).queryByRole("button", { name: "Перейти" })).toBeNull();
  });

  it("«Перейти» — новая пара токенов и главная выбранной компании", async () => {
    const user = userEvent.setup();
    let profile = manyCompanies();
    let body: unknown;
    signedInAs(profile);
    server.use(
      http.get("/api/v1/auth/me", () => HttpResponse.json(profile)),
      http.post("/api/v1/auth/switch-company", async ({ request }) => {
        body = await request.json();
        profile = me({
          company: { tenant_id: "t-2", member_id: "m-2", name: "ООО «Север»", role: "employee" },
          companies: profile.companies,
        });
        return HttpResponse.json(tokens(2));
      }),
    );
    const { router } = renderApp("/settings/companies");

    await screen.findByRole("list", { name: "Ваши компании" });
    await user.click(within(row("Север")).getByRole("button", { name: "Перейти" }));

    await waitFor(() => expect(router.state.location.pathname).toBe("/"));
    expect(body).toEqual({ tenant_id: "t-2" });
    expect(
      await screen.findByRole("button", { name: /^Компания: ООО «Север»/ }),
    ).toBeInTheDocument();
  });

  it("без компаний — подсказка вступить или подключить свою", async () => {
    signedInAs(loneMe());
    renderApp("/settings/companies");

    expect(await screen.findByText(/Вы пока не состоите ни в одной компании/)).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Вступить по приглашению" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Подключить свою компанию" })).toBeInTheDocument();
  });
});

describe("компании: выход", () => {
  it("после подтверждения; последнему администратору — объяснение сервера", async () => {
    const user = userEvent.setup();
    let profile = manyCompanies();
    let attempts = 0;
    let body: unknown;
    signedInAs(profile);
    server.use(
      http.get("/api/v1/auth/me", () => HttpResponse.json(profile)),
      http.post("/api/v1/account/leave", async ({ request }) => {
        attempts += 1;
        body = await request.json();
        if (attempts === 1) {
          return HttpResponse.json(
            {
              detail: "Вы единственный администратор компании — назначьте другого, прежде чем уйти",
              code: "last_admin",
            },
            { status: 409 },
          );
        }
        profile = me({ companies: profile.companies.filter((item) => item.tenant_id !== "t-2") });
        return new HttpResponse(null, { status: 204 });
      }),
    );
    renderApp("/settings/companies");

    await screen.findByRole("list", { name: "Ваши компании" });
    await user.click(within(row("Север")).getByRole("button", { name: "Выйти из компании" }));
    const dialog = await screen.findByRole("dialog", { name: "Выйти из «ООО «Север»»?" });
    expect(within(dialog).getByText(/удалятся через 30 дней/)).toBeInTheDocument();
    expect(attempts).toBe(0);

    await user.click(within(dialog).getByRole("button", { name: "Выйти" }));
    expect(
      await within(dialog).findByText(/Вы единственный администратор компании/),
    ).toBeInTheDocument();
    expect(body).toEqual({ tenant_id: "t-2" });

    await user.click(within(dialog).getByRole("button", { name: "Выйти" }));
    expect(await screen.findByText("Вы вышли из «ООО «Север»»")).toBeInTheDocument();
    await waitFor(() => expect(screen.queryByText("ООО «Север»")).not.toBeInTheDocument());
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });
});

describe("компании: приглашение", () => {
  it("код приглашения сохраняется для /join, в адрес не попадает", async () => {
    const user = userEvent.setup();
    signedInAs(loneMe());
    const { router } = renderApp("/settings/companies");

    await user.type(await screen.findByLabelText("Ссылка или код приглашения"), " K7QM-4XPA ");
    await user.click(screen.getByRole("button", { name: "Продолжить" }));

    await waitFor(() => expect(router.state.location.pathname).toBe("/join"));
    expect(router.state.location.search).toBe("");
    expect(router.state.location.hash).toBe("");
    expect(pendingInvite()).toBe("K7QM-4XPA");
  });

  it("из ссылки берёт токен после «#»; пустое поле не отправляет", async () => {
    const user = userEvent.setup();
    signedInAs(loneMe());
    const { router } = renderApp("/settings/companies");

    await user.click(await screen.findByRole("button", { name: "Продолжить" }));
    expect(await screen.findByText(/Вставьте ссылку-приглашение/)).toBeInTheDocument();
    expect(router.state.location.pathname).toBe("/settings/companies");

    await user.type(
      screen.getByLabelText("Ссылка или код приглашения"),
      "https://krontoai.ru/join#tok_0123456789abcdef",
    );
    await user.click(screen.getByRole("button", { name: "Продолжить" }));
    await waitFor(() => expect(router.state.location.pathname).toBe("/join"));
    expect(pendingInvite()).toBe("tok_0123456789abcdef");
  });
});

describe("компании: заявка на подключение", () => {
  it("отправляет заявку и показывает её на рассмотрении", async () => {
    const user = userEvent.setup();
    let list: Request[] = [];
    let body: unknown;
    signedInAs(loneMe());
    server.use(
      http.get("/api/v1/account/company-requests", () => HttpResponse.json(list)),
      http.post("/api/v1/account/company-requests", async ({ request: req }) => {
        body = await req.json();
        list = [request()];
        return HttpResponse.json(list[0], { status: 201 });
      }),
    );
    renderApp("/settings/companies");

    const section = await screen.findByRole("region", { name: "Подключить свою компанию" });
    expect(within(section).getByText(/команда kronto/)).toBeInTheDocument();
    // Пробный месяц — только первой пилотной компании (ТЗ §2): на сайте не обещаем.
    expect(within(section).queryByText(/Расширенный/)).not.toBeInTheDocument();
    await user.type(await within(section).findByLabelText("Название компании"), "  ООО  «Север» ");
    await user.type(within(section).getByLabelText(/Сколько сотрудников/), "25");
    await user.click(within(section).getByRole("button", { name: "Отправить заявку" }));

    expect(await screen.findByText("Заявка отправлена")).toBeInTheDocument();
    expect(body).toEqual({ company_name: "ООО «Север»", seats: 25, comment: null });
    expect(await within(section).findByText("на рассмотрении")).toBeInTheDocument();
    expect(within(section).getByText("25 мест")).toBeInTheDocument();
    expect(within(section).getByText(/Мы напишем на anna@meridian-stroy.ru/)).toBeInTheDocument();
    expect(within(section).queryByLabelText("Название компании")).not.toBeInTheDocument();
  });

  it("заявка уже есть — сообщение сервера и её строка в списке", async () => {
    const user = userEvent.setup();
    let list: Request[] = [];
    signedInAs(loneMe());
    server.use(
      http.get("/api/v1/account/company-requests", () => HttpResponse.json(list)),
      http.post("/api/v1/account/company-requests", () => {
        list = [request({ company_name: "ООО «Север» (из другой вкладки)" })];
        return HttpResponse.json(
          {
            detail: "Ваша заявка уже на рассмотрении — мы скоро ответим",
            code: "company_request_exists",
          },
          { status: 409 },
        );
      }),
    );
    renderApp("/settings/companies");

    const section = await screen.findByRole("region", { name: "Подключить свою компанию" });
    await user.type(await within(section).findByLabelText("Название компании"), "ООО «Север»");
    await user.click(within(section).getByRole("button", { name: "Отправить заявку" }));

    expect(
      await screen.findByText("Ваша заявка уже на рассмотрении — мы скоро ответим"),
    ).toBeInTheDocument();
    expect(await within(section).findByText("ООО «Север» (из другой вкладки)")).toBeInTheDocument();
  });

  it("проверяет поля до отправки", async () => {
    const user = userEvent.setup();
    let created = false;
    signedInAs(loneMe());
    server.use(
      http.post("/api/v1/account/company-requests", () => {
        created = true;
        return HttpResponse.json(request(), { status: 201 });
      }),
    );
    renderApp("/settings/companies");

    const section = await screen.findByRole("region", { name: "Подключить свою компанию" });
    await user.type(await within(section).findByLabelText("Название компании"), "А");
    await user.type(within(section).getByLabelText(/Сколько сотрудников/), "0");
    await user.click(within(section).getByRole("button", { name: "Отправить заявку" }));

    expect(within(section).getByText("Укажите название компании.")).toBeInTheDocument();
    expect(within(section).getByText("Целое число от 1 до 10 000.")).toBeInTheDocument();
    expect(created).toBe(false);
  });

  it("отменяет заявку на рассмотрении — форма возвращается", async () => {
    const user = userEvent.setup();
    let list = [request()];
    let cancelled: string | undefined;
    signedInAs(loneMe());
    server.use(
      http.get("/api/v1/account/company-requests", () => HttpResponse.json(list)),
      http.post("/api/v1/account/company-requests/:id/cancel", ({ params }) => {
        cancelled = params.id as string;
        list = [request({ status: "cancelled", decided_at: "2026-10-03T10:00:00Z" })];
        return HttpResponse.json(list[0]);
      }),
    );
    renderApp("/settings/companies");

    const section = await screen.findByRole("region", { name: "Подключить свою компанию" });
    const cancel = await within(section).findByRole("button", { name: "Отменить заявку" });
    // Пока заявка на рассмотрении, новую не подать.
    expect(within(section).queryByLabelText("Название компании")).not.toBeInTheDocument();
    await user.click(cancel);

    expect(await screen.findByText("Заявка отменена")).toBeInTheDocument();
    expect(cancelled).toBe("r-1");
    expect(await within(section).findByText("отменена")).toBeInTheDocument();
    expect(within(section).getByLabelText("Название компании")).toBeInTheDocument();
  });
});
