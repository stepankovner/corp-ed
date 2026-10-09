import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { describe, expect, it, vi } from "vitest";

import type { Schemas } from "../api/client";
import { adminMe } from "../test/fixtures";
import { renderApp } from "../test/render";
import { server } from "../test/server";

type Billing = Schemas["BillingResponse"];
type Invoice = Schemas["InvoiceResponse"];

const REQUISITES: Schemas["RequisitesResponse"] = {
  legal_name: "ООО «Меридиан Строй»",
  inn: "7707083893",
  kpp: "773601001",
  payer_type: "company",
  address: "Москва, Тверская, 1",
  documents_email: "buh@meridian.ru",
  updated_at: "2026-10-09T10:00:00Z",
};

function invoice(overrides: Partial<Invoice> = {}): Invoice {
  return {
    id: "i-1",
    number: "KR-00001",
    kind: "subscription",
    status: "awaiting_payment",
    payment_method: "invoice",
    amount_kopecks: 32_076_000,
    title: "Подписка kronto",
    purpose: "Оплата по счёту № KR-00001 от 09.10.2026 за доступ к сервису kronto. Без НДС",
    period_start: null,
    period_end: null,
    due_date: "2026-10-14",
    payment_url: null,
    has_pdf: true,
    created_at: "2026-10-09T10:00:00Z",
    paid_at: null,
    ...overrides,
  };
}

function billing(overrides: Partial<Billing> = {}): Billing {
  return {
    enabled: true,
    tariff: "base",
    seats: 30,
    seat_price_kopecks: 99_000,
    quotes: [
      { period: "month", discount_percent: 0, amount_kopecks: 2_970_000 },
      { period: "quarter", discount_percent: 5, amount_kopecks: 8_464_500 },
      { period: "year", discount_percent: 10, amount_kopecks: 32_076_000 },
    ],
    grace_days: 7,
    subscription: null,
    requisites: REQUISITES,
    invoices: [],
    acts: [],
    ...overrides,
  };
}

const PACKS: Schemas["CreditPackResponse"][] = [
  { code: "pack_500", credits: 500, price_kopecks: 149_000, valid_months: 12 },
];

function shell(data: Billing) {
  const chosen: unknown[] = [];
  const ordered: unknown[] = [];
  let state = data;
  server.use(
    http.get("/api/v1/auth/me", () => HttpResponse.json(adminMe())),
    http.get("/api/v1/usage", () =>
      HttpResponse.json({
        period_start: "2026-10-01T00:00:00+03:00",
        period_end: "2026-11-01T00:00:00+03:00",
        seats: 30,
        credits_per_seat: 420,
        pool: 12600,
        used: 0,
        remaining: 12600,
        exhausted: false,
        warn_at_percent: 80,
        warning: false,
        purchased: 0,
        purchased_expires_at: null,
        purchased_expiring: 0,
        stopped: false,
        avg_credits_per_question: 1,
      }),
    ),
    http.get("/api/v1/company", () =>
      HttpResponse.json({
        id: "t-1",
        name: "ООО «Меридиан Строй»",
        company_code: "meridian",
        logo_url: null,
        not_found_mode: "general",
        mfa_policy: "any",
        allow_remember_device: true,
        email_domains: [],
        chat_retention_months: 12,
        tariff: "base",
        seats: 30,
        members: 12,
        daily_credits_per_member: null,
      }),
    ),
    http.get("/api/v1/billing", () => HttpResponse.json(state)),
    http.post("/api/v1/billing/subscription", async ({ request }) => {
      const body = (await request.json()) as Schemas["SubscriptionChoiceRequest"];
      chosen.push(body);
      const issued =
        body.payment_method === "card"
          ? invoice({
              amount_kopecks: 2_970_000,
              payment_method: "card",
              payment_url: "https://merch.tochka.com/order/?uuid=1",
              has_pdf: false,
            })
          : invoice();
      state = { ...state, invoices: [issued] };
      return HttpResponse.json({ invoice: issued });
    }),
    http.get("/api/v1/credits/packs", () => HttpResponse.json(PACKS)),
    http.get("/api/v1/credits/orders", () => HttpResponse.json([])),
    http.post("/api/v1/credits/orders", async ({ request }) => {
      const body = (await request.json()) as Schemas["CreditOrderRequest"];
      ordered.push(body);
      return HttpResponse.json(
        {
          id: "o-1",
          number: 1,
          pack: "pack_500",
          credits: 500,
          amount_kopecks: 149_000,
          status: "awaiting_payment",
          payment_method: body.payment_method,
          created_at: "2026-10-09T10:00:00Z",
          paid_at: null,
          cancelled_at: null,
          invoice_id: "i-9",
          payment_url: "https://merch.tochka.com/order/?uuid=9",
        },
        { status: 201 },
      );
    }),
  );
  return { chosen, ordered };
}

describe("оплата подписки", () => {
  it("период со скидкой и счёт для юрлица", async () => {
    const user = userEvent.setup();
    const { chosen } = shell(billing());
    renderApp("/admin/tariff");

    const section = await screen.findByRole("region", { name: "Оплата подписки" });
    const year = within(section).getByRole("radio", { name: /Год/ });
    expect(year).toHaveAccessibleName(/−10 %/);
    expect(year).toHaveAccessibleName(/320\s760\s₽/);
    await user.click(year);
    await user.click(within(section).getByRole("button", { name: "Выставить счёт" }));

    expect(await within(section).findByText(/Счёт KR-00001 на 320\s760\s₽/)).toBeInTheDocument();
    expect(chosen).toEqual([{ period: "year", payment_method: "invoice" }]);

    const documents = await screen.findByRole("region", { name: "Счета и акты" });
    const table = await within(documents).findByRole("table", { name: "Счета" });
    expect(within(table).getByText("KR-00001")).toBeInTheDocument();
    expect(within(table).getByText("Ждёт оплаты")).toBeInTheDocument();

    server.use(
      http.get(
        "/api/v1/billing/invoices/i-1/pdf",
        () => new HttpResponse("%PDF-1.4", { headers: { "content-type": "application/pdf" } }),
      ),
    );
    const created = vi.spyOn(URL, "createObjectURL").mockReturnValue("blob:invoice");
    const revoked = vi.spyOn(URL, "revokeObjectURL").mockImplementation(() => undefined);
    const click = vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => {});
    await user.click(within(table).getByRole("button", { name: "Скачать счёт KR-00001" }));
    await waitFor(() => expect(click).toHaveBeenCalled());
    expect(created).toHaveBeenCalled();
    expect(revoked).toHaveBeenCalledWith("blob:invoice");
    vi.restoreAllMocks();
  });

  it("картой — только помесячно, ссылка на оплату", async () => {
    const user = userEvent.setup();
    const { chosen } = shell(billing({ requisites: null }));
    renderApp("/admin/tariff");

    const section = await screen.findByRole("region", { name: "Оплата подписки" });
    // Реквизитов нет — по умолчанию карта.
    expect(within(section).getByRole("radio", { name: /Картой или СБП/ })).toBeChecked();
    await user.click(within(section).getByRole("radio", { name: /Квартал/ }));
    // Квартал — только счётом, а для счёта нужны реквизиты.
    expect(within(section).getByRole("radio", { name: /Счёт для юрлица/ })).toBeChecked();
    expect(
      within(section).getByRole("link", { name: "заполните их в настройках" }),
    ).toHaveAttribute("href", "/admin/settings");
    expect(within(section).getByRole("button", { name: "Выставить счёт" })).toBeDisabled();

    await user.click(within(section).getByRole("radio", { name: /Картой или СБП/ }));
    expect(within(section).getByRole("radio", { name: /Месяц/ })).toBeChecked();
    await user.click(within(section).getByRole("button", { name: "Получить ссылку на оплату" }));
    const pay = await within(section).findByRole("link", { name: "Оплатить" });
    expect(pay).toHaveAttribute("href", "https://merch.tochka.com/order/?uuid=1");
    expect(pay).toHaveAttribute("target", "_blank");
    expect(chosen).toEqual([{ period: "month", payment_method: "card" }]);
  });

  it("оплаченная подписка: статус, сокращение мест, акты", async () => {
    shell(
      billing({
        subscription: {
          tariff: "base",
          seats: 30,
          next_seats: 20,
          period: "month",
          payment_method: "card",
          status: "overdue",
          current_start: "2026-09-09",
          current_end: "2026-10-09",
          card_amount_kopecks: 2_970_000,
        },
        acts: [
          {
            id: "a-1",
            number: 1,
            month: "2026-09-01",
            amount_kopecks: 2_970_000,
            created_at: "2026-10-01T06:00:00Z",
          },
        ],
      }),
    );
    renderApp("/admin/tariff");

    const section = await screen.findByRole("region", { name: "Оплата подписки" });
    expect(within(section).getByText("Просрочена")).toBeInTheDocument();
    expect(section).toHaveTextContent("Оплачено по 8 октября 2026 г.");
    expect(section).toHaveTextContent(/Автосписание 29\s700\s₽ раз в месяц/);
    expect(section).toHaveTextContent("Со следующего периода — 20 мест.");
    expect(within(section).getByText("Подписка не оплачена")).toBeInTheDocument();
    expect(
      within(section).getByRole("button", { name: "Сохранить для следующего периода" }),
    ).toBeInTheDocument();
    const acts = screen.getByRole("table", { name: "Акты" });
    expect(within(acts).getByText("сентябрь 2026")).toBeInTheDocument();
    expect(
      within(acts).getByRole("button", { name: "Скачать акт за сентябрь 2026" }),
    ).toBeInTheDocument();
  });

  it("пакет кредитов картой: способ оплаты и ссылка", async () => {
    const user = userEvent.setup();
    const { ordered } = shell(billing());
    renderApp("/admin/tariff");

    const packs = await screen.findByRole("region", { name: "Пакеты кредитов" });
    expect(
      await within(packs).findByText(/кредиты зачисляются сразу после оплаты/),
    ).toBeInTheDocument();
    await user.click(await within(packs).findByRole("button", { name: /Купить 500 кредитов/ }));
    const dialog = screen.getByRole("dialog", { name: "Купить пакет кредитов" });
    await user.click(within(dialog).getByRole("radio", { name: /Картой или СБП/ }));
    await user.click(within(dialog).getByRole("button", { name: "Заказать" }));

    const pay = await within(dialog).findByRole("link", { name: "Оплатить" });
    expect(pay).toHaveAttribute("href", "https://merch.tochka.com/order/?uuid=9");
    expect(ordered).toEqual([{ pack: "pack_500", payment_method: "card" }]);
    expect(screen.getByText(/Подписка и пакеты оплачиваются счётом/)).toBeInTheDocument();
  });

  it("корпоративный тариф — по договору", async () => {
    shell(billing({ tariff: "enterprise", seat_price_kopecks: null, quotes: [] }));
    renderApp("/admin/tariff");
    const section = await screen.findByRole("region", { name: "Оплата подписки" });
    expect(section).toHaveTextContent("Корпоративный тариф оплачивается по договору");
    expect(within(section).queryByRole("radio")).not.toBeInTheDocument();
  });
});

describe("реквизиты компании", () => {
  it("проверяет вид ИНН и КПП, показывает ошибку сервера у поля", async () => {
    const user = userEvent.setup();
    const saved: unknown[] = [];
    shell(billing());
    server.use(
      http.put("/api/v1/company/requisites", async ({ request }) => {
        const body = (await request.json()) as Schemas["RequisitesRequest"];
        saved.push(body);
        if (body.inn === "7707083894") {
          return HttpResponse.json(
            {
              detail: "ИНН с ошибкой: не сходятся контрольные цифры",
              code: "invalid_requisites",
              field: "inn",
            },
            { status: 422 },
          );
        }
        return HttpResponse.json({ ...REQUISITES, ...body, payer_type: "company" });
      }),
    );
    renderApp("/admin/settings");

    const section = await screen.findByRole("region", { name: "Реквизиты для счетов" });
    await user.type(
      await within(section).findByLabelText("Название организации или ИП"),
      "ООО «Меридиан»",
    );
    await user.type(within(section).getByLabelText("ИНН"), "77070838");
    await user.type(within(section).getByLabelText("Юридический адрес"), "Москва, Тверская, 1");
    await user.click(within(section).getByRole("button", { name: "Сохранить реквизиты" }));
    expect(
      within(section).getByText("ИНН — 10 цифр у организации или 12 у ИП."),
    ).toBeInTheDocument();
    expect(within(section).getByText("КПП — 9 знаков, например 773601001.")).toBeInTheDocument();
    expect(saved).toEqual([]);

    await user.type(within(section).getByLabelText("ИНН"), "94");
    await user.type(within(section).getByLabelText("КПП"), "773601001");
    await user.click(within(section).getByRole("button", { name: "Сохранить реквизиты" }));
    expect(
      await within(section).findByText("ИНН с ошибкой: не сходятся контрольные цифры"),
    ).toBeInTheDocument();

    const inn = within(section).getByLabelText("ИНН");
    await user.clear(inn);
    await user.type(inn, "7707083893");
    await user.click(within(section).getByRole("button", { name: "Сохранить реквизиты" }));
    expect(await screen.findByText("Реквизиты сохранены")).toBeInTheDocument();
    expect(saved.at(-1)).toEqual({
      legal_name: "ООО «Меридиан»",
      inn: "7707083893",
      kpp: "773601001",
      address: "Москва, Тверская, 1",
      documents_email: null,
    });
  });

  it("у ИП поля КПП нет", async () => {
    const user = userEvent.setup();
    shell(billing());
    renderApp("/admin/settings");
    const section = await screen.findByRole("region", { name: "Реквизиты для счетов" });
    expect(await within(section).findByLabelText("КПП")).toBeInTheDocument();
    await user.type(within(section).getByLabelText("ИНН"), "500100732259");
    expect(within(section).queryByLabelText("КПП")).not.toBeInTheDocument();
  });
});
