import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { Schemas } from "../api/client";
import { adminMe } from "../test/fixtures";
import { renderApp } from "../test/render";
import { server } from "../test/server";

type CompanyRequest = Schemas["StaffRequestResponse"];

function companyRequest(overrides: Partial<CompanyRequest> = {}): CompanyRequest {
  return {
    id: "r-1",
    company_name: "Северный ветер",
    seats: 25,
    comment: "Хотим начать с отдела продаж",
    status: "new",
    created_at: "2026-10-03T09:30:00Z",
    decided_at: null,
    tenant_id: null,
    applicant_email: "ivan@sever.ru",
    applicant_name: "Иван Петров",
    ...overrides,
  };
}

function createdCompany(name: string): Schemas["StaffCompanyResponse"] {
  return {
    id: "t-new",
    name,
    company_code: "severnyy-veter-1a2b",
    is_active: true,
    tariff: "base",
    seats: 10,
    pilot_until: null,
    members: 1,
    pending: 0,
    admins: ["ivan@sever.ru"],
    credits_used: 0,
    pool: 4200,
    questions_month: 0,
    last_question_at: null,
    documents: 0,
    connectors: 0,
  };
}

/** Сервер панели в памяти: заявки, их решения и обзор в шапке; calls — тела запросов. */
function mockStaff(initial: CompanyRequest[]) {
  let requests = initial;
  const calls: { path: string; body: unknown }[] = [];
  const decide = (id: string, status: CompanyRequest["status"]) => {
    requests = requests.map((item) =>
      item.id === id ? { ...item, status, decided_at: "2026-10-04T09:00:00Z" } : item,
    );
  };
  server.use(
    http.get("/api/v1/auth/me", () => HttpResponse.json(adminMe({ staff: true }))),
    http.get("/api/v1/usage", () => HttpResponse.json({ warning: false })),
    http.get("/api/v1/staff/overview", () =>
      HttpResponse.json({
        companies: 1,
        active_companies: 1,
        pilots_ending: 0,
        requests_new: requests.filter((item) => item.status === "new").length,
        accounts: 5,
      }),
    ),
    http.get("/api/v1/staff/requests", ({ request }) => {
      const status = new URL(request.url).searchParams.get("status");
      return HttpResponse.json(
        status === "all" ? requests : requests.filter((item) => item.status === "new"),
      );
    }),
    http.post("/api/v1/staff/requests/:id/approve", async ({ request, params }) => {
      const id = String(params.id);
      calls.push({ path: `approve/${id}`, body: await request.json() });
      const item = requests.find((r) => r.id === id);
      if (!item || item.status !== "new") {
        return HttpResponse.json({ detail: "Заявка уже рассмотрена" }, { status: 409 });
      }
      decide(id, "approved");
      return HttpResponse.json(createdCompany(item.company_name));
    }),
    http.post("/api/v1/staff/requests/:id/reject", ({ params }) => {
      const id = String(params.id);
      calls.push({ path: `reject/${id}`, body: null });
      decide(id, "rejected");
      return new HttpResponse(null, { status: 204 });
    }),
  );
  return { calls };
}

beforeEach(() => {
  // Только дата: срок пилота сверяется с «сегодня»; таймеры остаются настоящими.
  vi.useFakeTimers({ toFake: ["Date"], now: new Date("2026-10-04T09:00:00Z") });
});

afterEach(() => {
  vi.useRealTimers();
});

describe("панель: заявки на компании", () => {
  it("показывает новую заявку; одобрение уходит с тарифом, местами и пилотом", async () => {
    const user = userEvent.setup();
    const { calls } = mockStaff([companyRequest()]);
    renderApp("/staff/requests");

    const card = (await screen.findByRole("heading", { name: "Северный ветер" })).closest("li")!;
    expect(document.title).toBe("Заявки — kronto");
    expect(screen.getByRole("button", { name: "Новые" })).toHaveAttribute("aria-pressed", "true");
    expect(await screen.findByText(/новых заявок: 1/)).toBeInTheDocument();
    expect(within(card).getByText(/Иван Петров/)).toBeInTheDocument();
    expect(within(card).getByRole("link", { name: "ivan@sever.ru" })).toHaveAttribute(
      "href",
      "mailto:ivan@sever.ru",
    );
    expect(within(card).getByText("25 мест")).toBeInTheDocument();
    expect(within(card).getByText("Хотим начать с отдела продаж")).toBeInTheDocument();

    await user.click(within(card).getByRole("button", { name: "Одобрить: Северный ветер" }));
    const dialog = screen.getByRole("dialog", { name: "Одобрить заявку" });
    expect(dialog).toHaveAccessibleDescription(
      "Создадим компанию «Северный ветер». Иван Петров (ivan@sever.ru) станет её администратором и получит письмо со ссылкой на вход.",
    );
    expect(within(dialog).getByRole("radio", { name: /Базовый/ })).toBeChecked();
    const seats = within(dialog).getByLabelText("Рабочих мест");
    expect(seats).toHaveValue(25);

    await user.click(within(dialog).getByRole("radio", { name: /Расширенный/ }));
    await user.clear(seats);
    await user.type(seats, "30");
    await user.type(within(dialog).getByLabelText(/Пилот до/), "2026-11-03");
    await user.click(within(dialog).getByRole("button", { name: "Создать компанию" }));

    expect(await screen.findByText("Компания «Северный ветер» создана")).toBeInTheDocument();
    expect(calls).toEqual([
      { path: "approve/r-1", body: { tariff: "extended", seats: 30, pilot_until: "2026-11-03" } },
    ]);
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(await screen.findByText("Новых заявок нет")).toBeInTheDocument();
    // Шапка панели перечитана вместе со списком.
    expect(await screen.findByText(/новых заявок нет/)).toBeInTheDocument();
  });

  it("без мест в заявке предлагает 10; места проверяет до отправки", async () => {
    const user = userEvent.setup();
    const { calls } = mockStaff([companyRequest({ seats: null, comment: null })]);
    renderApp("/staff/requests");

    const card = (await screen.findByRole("heading", { name: "Северный ветер" })).closest("li")!;
    expect(within(card).getByText("мест не указали")).toBeInTheDocument();
    await user.click(within(card).getByRole("button", { name: "Одобрить: Северный ветер" }));
    const dialog = screen.getByRole("dialog", { name: "Одобрить заявку" });
    const seats = within(dialog).getByLabelText("Рабочих мест");
    expect(seats).toHaveValue(10);

    await user.clear(seats);
    await user.click(within(dialog).getByRole("button", { name: "Создать компанию" }));
    expect(within(dialog).getByRole("alert")).toHaveTextContent("Укажите целое число от 1");
    expect(calls).toEqual([]);

    await user.type(seats, "10");
    await user.click(within(dialog).getByRole("button", { name: "Создать компанию" }));
    expect(await screen.findByText("Компания «Северный ветер» создана")).toBeInTheDocument();
    expect(calls).toEqual([{ path: "approve/r-1", body: { tariff: "base", seats: 10 } }]);
  });

  it("отклоняет заявку после подтверждения", async () => {
    const user = userEvent.setup();
    const { calls } = mockStaff([
      companyRequest(),
      companyRequest({ id: "r-2", company_name: "Восток", applicant_email: "olga@vostok.ru" }),
    ]);
    renderApp("/staff/requests");

    const card = (await screen.findByRole("heading", { name: "Восток" })).closest("li")!;
    await user.click(within(card).getByRole("button", { name: "Отклонить: Восток" }));
    const dialog = screen.getByRole("dialog", { name: "Отклонить заявку?" });
    expect(dialog).toHaveTextContent("На olga@vostok.ru уйдёт письмо");
    expect(calls).toEqual([]);
    await user.click(within(dialog).getByRole("button", { name: "Отклонить" }));

    expect(await screen.findByText("Заявка «Восток» отклонена")).toBeInTheDocument();
    expect(calls).toEqual([{ path: "reject/r-2", body: null }]);
    await waitFor(() =>
      expect(screen.queryByRole("heading", { name: "Восток" })).not.toBeInTheDocument(),
    );
    expect(screen.getByRole("heading", { name: "Северный ветер" })).toBeInTheDocument();
  });

  it("«Все» — с решениями; заявку, рассмотренную в другой вкладке, не одобрить дважды", async () => {
    const user = userEvent.setup();
    mockStaff([
      companyRequest(),
      companyRequest({ id: "r-2", company_name: "Восток", status: "approved" }),
      companyRequest({ id: "r-3", company_name: "Запад", status: "rejected" }),
      companyRequest({ id: "r-4", company_name: "Юг", status: "cancelled" }),
    ]);
    renderApp("/staff/requests");

    await screen.findByRole("heading", { name: "Северный ветер" });
    expect(screen.queryByRole("heading", { name: "Восток" })).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Все" }));

    const approved = (await screen.findByRole("heading", { name: "Восток" })).closest("li")!;
    expect(within(approved).getByText("одобрена")).toBeInTheDocument();
    expect(within(approved).queryByRole("button")).not.toBeInTheDocument();
    const rejected = screen.getByRole("heading", { name: "Запад" }).closest("li")!;
    expect(within(rejected).getByText("отклонена")).toBeInTheDocument();
    const cancelled = screen.getByRole("heading", { name: "Юг" }).closest("li")!;
    expect(within(cancelled).getByText("отозвана")).toBeInTheDocument();

    // Заявку уже одобрили коллеги: сервер отвечает 409 — показываем его ответ.
    server.use(
      http.post("/api/v1/staff/requests/:id/approve", () =>
        HttpResponse.json({ detail: "Заявка уже рассмотрена" }, { status: 409 }),
      ),
    );
    const fresh = screen.getByRole("heading", { name: "Северный ветер" }).closest("li")!;
    await user.click(within(fresh).getByRole("button", { name: "Одобрить: Северный ветер" }));
    const dialog = screen.getByRole("dialog", { name: "Одобрить заявку" });
    await user.click(within(dialog).getByRole("button", { name: "Создать компанию" }));
    expect(await within(dialog).findByRole("alert")).toHaveTextContent("Заявка уже рассмотрена");
  });

  it("пусто — так и говорит", async () => {
    mockStaff([]);
    renderApp("/staff/requests");
    expect(await screen.findByText("Новых заявок нет")).toBeInTheDocument();
  });
});
