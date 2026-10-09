import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { describe, expect, it } from "vitest";

import type { Schemas } from "../api/client";
import { adminMe } from "../test/fixtures";
import { renderApp } from "../test/render";
import { server } from "../test/server";

type Order = Schemas["StaffCreditOrderResponse"];
type Company = Schemas["StaffCompanyResponse"];

function order(overrides: Partial<Order> = {}): Order {
  return {
    id: "o-1",
    number: 1,
    pack: "pack_2000",
    credits: 2000,
    amount_kopecks: 549_000,
    status: "awaiting_payment",
    payment_method: "invoice",
    created_at: "2026-10-09T10:00:00Z",
    paid_at: null,
    cancelled_at: null,
    tenant_id: "t-1",
    company_name: "Меридиан Строй",
    company_code: "meridian",
    ...overrides,
  };
}

const COMPANY: Company = {
  id: "t-1",
  name: "Меридиан Строй",
  company_code: "meridian",
  is_active: true,
  tariff: "base",
  seats: 30,
  pilot_until: null,
  members: 12,
  pending: 0,
  admins: ["anna@meridian.ru"],
  credits_used: 3150,
  pool: 12600,
  purchased_credits: 0,
  questions_month: 840,
  last_question_at: null,
  documents: 48,
  connectors: 2,
};

function shell(initial: Order[]) {
  let orders = initial;
  const asked: (string | null)[] = [];
  const actions: string[] = [];
  const grants: unknown[] = [];
  server.use(
    http.get("/api/v1/auth/me", () => HttpResponse.json(adminMe({ staff: true }))),
    http.get("/api/v1/usage", () => HttpResponse.json({ warning: false })),
    http.get("/api/v1/notifications", () => HttpResponse.json({ items: [], unread: 0 })),
    http.get("/api/v1/staff/overview", () =>
      HttpResponse.json({
        companies: 1,
        active_companies: 1,
        pilots_ending: 0,
        requests_new: 0,
        accounts: 3,
      }),
    ),
    http.get("/api/v1/staff/companies", () => HttpResponse.json([COMPANY])),
    http.get("/api/v1/staff/credit-orders", ({ request }) => {
      const status = new URL(request.url).searchParams.get("status");
      asked.push(status);
      return HttpResponse.json(
        status === "all" ? orders : orders.filter((item) => item.status === "awaiting_payment"),
      );
    }),
    http.post("/api/v1/staff/companies/:tenantId/credit-orders/:orderId/:action", ({ params }) => {
      const action = String(params.action);
      actions.push(`${String(params.tenantId)}/${String(params.orderId)}/${action}`);
      orders = orders.map((item) =>
        item.id === params.orderId
          ? { ...item, status: action === "paid" ? "paid" : "cancelled" }
          : item,
      );
      return HttpResponse.json(orders.find((item) => item.id === params.orderId));
    }),
    http.post("/api/v1/staff/companies/:tenantId/credits", async ({ request }) => {
      grants.push(await request.json());
      return HttpResponse.json(
        { id: "g-1", credits: 300, expires_at: "2027-10-09T10:00:00Z" },
        { status: 201 },
      );
    }),
  );
  return { asked, actions, grants };
}

describe("кредиты в нашей панели", () => {
  it("заказ ждёт оплаты: «Оплачен» зачисляет кредиты после подтверждения", async () => {
    const user = userEvent.setup();
    const { actions } = shell([order(), order({ id: "o-2", number: 2, credits: 500 })]);
    renderApp("/staff/credits");

    const list = await screen.findByRole("list", { name: "Заказы пакетов" });
    expect(document.title).toBe("Кредиты — kronto");
    const card = within(list).getByText("Меридиан Строй · заказ № 1").closest("li");
    if (!card) throw new Error("no card");
    expect(card).toHaveTextContent("2 000 кредитов · 5 490 ₽");
    expect(card).toHaveTextContent("meridian-1");

    await user.click(within(card).getByRole("button", { name: "Оплачен" }));
    const dialog = screen.getByRole("dialog", { name: "Заказ № 1 оплачен?" });
    await user.click(within(dialog).getByRole("button", { name: "Оплачен, зачислить" }));

    expect(await screen.findByText(/Кредиты зачислены: Меридиан Строй/)).toBeInTheDocument();
    expect(actions).toEqual(["t-1/o-1/paid"]);
    await waitFor(() =>
      expect(screen.queryByText("Меридиан Строй · заказ № 1")).not.toBeInTheDocument(),
    );
  });

  it("неоплаченный заказ можно отменить; «Все» показывает и закрытые", async () => {
    const user = userEvent.setup();
    const { actions, asked } = shell([order(), order({ id: "o-0", number: 0, status: "paid" })]);
    renderApp("/staff/credits");

    const list = await screen.findByRole("list", { name: "Заказы пакетов" });
    await user.click(within(list).getByRole("button", { name: "Отменить" }));
    const dialog = screen.getByRole("dialog", { name: "Отменить заказ № 1?" });
    await user.click(within(dialog).getByRole("button", { name: "Отменить заказ" }));
    expect(await screen.findByText(/Заказ № 1 отменён/)).toBeInTheDocument();
    expect(actions).toEqual(["t-1/o-1/cancel"]);

    await user.click(screen.getByRole("button", { name: "Все" }));
    expect(await screen.findByText("Меридиан Строй · заказ № 0")).toBeInTheDocument();
    expect(asked).toContain("all");
  });

  it("ручное начисление: компания, кредиты и комментарий", async () => {
    const user = userEvent.setup();
    const { grants } = shell([]);
    renderApp("/staff/credits");

    expect(await screen.findByText("Заказов, ждущих оплаты, нет")).toBeInTheDocument();
    const form = screen.getByRole("region", { name: "Начислить кредиты" });
    await user.click(within(form).getByRole("button", { name: "Начислить" }));
    expect(within(form).getByText("Выберите компанию.")).toBeInTheDocument();
    expect(grants).toEqual([]);

    await user.selectOptions(within(form).getByLabelText("Компания"), "t-1");
    await user.type(within(form).getByLabelText("Кредитов"), "300");
    await user.type(within(form).getByLabelText("Комментарий"), "Компенсация за сбой 08.10");
    await user.click(within(form).getByRole("button", { name: "Начислить" }));

    expect(await screen.findByText(/Начислено 300 кредитов: Меридиан Строй/)).toBeInTheDocument();
    expect(grants).toEqual([{ credits: 300, comment: "Компенсация за сбой 08.10" }]);
  });
});
