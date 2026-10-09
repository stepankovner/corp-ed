import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { describe, expect, it } from "vitest";

import type { Schemas } from "../api/client";
import { adminMe } from "../test/fixtures";
import { renderApp } from "../test/render";
import { server } from "../test/server";

type Payment = Schemas["StaffPaymentResponse"];
type Invoice = Schemas["StaffInvoiceResponse"];

const PAYMENT: Payment = {
  id: "p-1",
  kind: "incoming",
  status: "mismatch",
  problem: "amount_differs",
  amount_kopecks: 2_900_000,
  payer_inn: "7707083893",
  payer_name: "ООО «Меридиан Строй»",
  purpose: "Оплата по счёту KR-00001",
  tenant_id: "t-1",
  company_name: "Меридиан Строй",
  invoice_id: "i-1",
  note: null,
  created_at: "2026-10-09T10:00:00Z",
};

const INVOICE: Invoice = {
  id: "i-1",
  number: "KR-00001",
  kind: "subscription",
  status: "awaiting_payment",
  payment_method: "invoice",
  amount_kopecks: 2_970_000,
  title: "Подписка kronto",
  purpose: "Оплата по счёту № KR-00001",
  period_start: null,
  period_end: null,
  due_date: "2026-10-14",
  payment_url: null,
  has_pdf: true,
  created_at: "2026-10-09T09:00:00Z",
  paid_at: null,
  tenant_id: "t-1",
  company_name: "Меридиан Строй",
  company_code: "meridian",
  payer_name: "ООО «Меридиан Строй»",
  payer_inn: "7707083893",
};

function shell({ enabled = true }: { enabled?: boolean } = {}) {
  const resolved: unknown[] = [];
  const marked: string[] = [];
  let payments = [PAYMENT];
  let invoices = [INVOICE];
  server.use(
    http.get("/api/v1/auth/me", () => HttpResponse.json(adminMe({ staff: true }))),
    http.get("/api/v1/usage", () => HttpResponse.json({ warning: false })),
    http.get("/api/v1/staff/overview", () =>
      HttpResponse.json({
        companies: 1,
        active_companies: 1,
        pilots_ending: 0,
        requests_new: 0,
        accounts: 3,
      }),
    ),
    http.get("/api/v1/staff/billing", () =>
      HttpResponse.json({
        enabled,
        provider: enabled ? "tochka" : "none",
        subscriptions: [
          {
            tariff: "base",
            seats: 30,
            next_seats: 20,
            period: "month",
            payment_method: "invoice",
            status: "overdue",
            current_start: "2026-09-09",
            current_end: "2026-10-09",
            card_amount_kopecks: null,
            tenant_id: "t-1",
            company_name: "Меридиан Строй",
            company_code: "meridian",
            is_active: true,
          },
        ],
      }),
    ),
    http.get("/api/v1/staff/billing/payments", () => HttpResponse.json(payments)),
    http.get("/api/v1/staff/billing/invoices", () => HttpResponse.json(invoices)),
    http.post("/api/v1/staff/billing/payments/:id/resolve", async ({ request }) => {
      resolved.push(await request.json());
      payments = [];
      invoices = [];
      return new HttpResponse(null, { status: 204 });
    }),
    http.post("/api/v1/staff/companies/:tenantId/invoices/:invoiceId/:action", ({ params }) => {
      marked.push(String(params.action));
      invoices = [];
      return HttpResponse.json({ ...INVOICE, status: "paid" });
    }),
  );
  return { resolved, marked };
}

describe("оплата в панели", () => {
  it("платёж с другой суммой: команда зачитывает его в счёт", async () => {
    const user = userEvent.setup();
    const { resolved } = shell();
    renderApp("/staff/payments");

    const list = await screen.findByRole("list", { name: "Платежи на разбор" });
    const card = within(list).getByRole("listitem");
    expect(card).toHaveTextContent("сумма не совпала со счётом");
    expect(card).toHaveTextContent("Оплата по счёту KR-00001");

    const subscriptions = screen.getByRole("table", { name: "Подписки" });
    expect(within(subscriptions).getByText("Просрочена")).toBeInTheDocument();
    expect(subscriptions).toHaveTextContent("30 → 20");

    await user.click(within(card).getByRole("button", { name: "Разобрать" }));
    const dialog = screen.getByRole("dialog", { name: "Разобрать платёж" });
    const select = await within(dialog).findByRole("combobox", { name: "Счёт" });
    await waitFor(() => expect(select).toHaveValue("i-1"));
    expect(within(dialog).getByText(/Сумма платежа не совпадает со счётом/)).toBeInTheDocument();
    await user.type(within(dialog).getByLabelText(/Комментарий/), "Доплатят отдельно");
    await user.click(within(dialog).getByRole("button", { name: "Зачесть" }));

    expect(await screen.findByText("Платёж зачтён, услуга зачислена")).toBeInTheDocument();
    expect(resolved).toEqual([{ tenant_id: "t-1", invoice_id: "i-1", note: "Доплатят отдельно" }]);
    expect(await screen.findByText("Разбирать нечего")).toBeInTheDocument();
  });

  it("закрыть без зачёта — только с комментарием", async () => {
    const user = userEvent.setup();
    const { resolved } = shell();
    renderApp("/staff/payments");

    const list = await screen.findByRole("list", { name: "Платежи на разбор" });
    await user.click(within(list).getByRole("button", { name: "Разобрать" }));
    const dialog = screen.getByRole("dialog", { name: "Разобрать платёж" });
    const select = await within(dialog).findByRole("combobox", { name: "Счёт" });
    await user.selectOptions(select, "");
    await user.click(within(dialog).getByRole("button", { name: "Закрыть без зачёта" }));
    expect(
      within(dialog).getByText("Без зачёта нужен комментарий: почему платёж закрыт."),
    ).toBeInTheDocument();
    expect(resolved).toEqual([]);

    await user.type(within(dialog).getByLabelText(/Комментарий/), "Вернули плательщику");
    await user.click(within(dialog).getByRole("button", { name: "Закрыть без зачёта" }));
    await waitFor(() => expect(resolved).toEqual([{ note: "Вернули плательщику" }]));
  });

  it("счёт оплачен вручную", async () => {
    const user = userEvent.setup();
    const { marked } = shell();
    renderApp("/staff/payments");

    const list = await screen.findByRole("list", { name: "Счета, ждущие оплаты" });
    expect(list).toHaveTextContent("Меридиан Строй · KR-00001");
    await user.click(within(list).getByRole("button", { name: "Оплачен" }));
    const confirm = screen.getByRole("dialog", { name: "Счёт KR-00001 оплачен?" });
    await user.click(within(confirm).getByRole("button", { name: "Оплачен" }));
    await waitFor(() => expect(marked).toEqual(["paid"]));
    expect(await screen.findByText("Неоплаченных счетов нет")).toBeInTheDocument();
  });

  it("банк не подключён — подсказка про ручную отметку", async () => {
    shell({ enabled: false });
    renderApp("/staff/payments");
    expect(await screen.findByText("Банк не подключён")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Оплата" })).toBeInTheDocument();
  });
});
