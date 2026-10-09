import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { describe, expect, it } from "vitest";

import type { Schemas } from "../api/client";
import { adminMe } from "../test/fixtures";
import { renderApp } from "../test/render";
import { server } from "../test/server";

function companySettings(
  overrides: Partial<Schemas["CompanySettingsResponse"]> = {},
): Schemas["CompanySettingsResponse"] {
  return {
    id: "t-1",
    name: "ООО «Меридиан Строй»",
    company_code: "meridian",
    logo_url: null,
    not_found_mode: "general",
    mfa_policy: "any",
    allow_remember_device: true,
    email_domains: [],
    tariff: "base",
    seats: 30,
    members: 12,
    daily_credits_per_member: null,
    ...overrides,
  };
}

function usage(overrides: Partial<Schemas["UsageResponse"]> = {}): Schemas["UsageResponse"] {
  return {
    period_start: "2026-10-01T00:00:00+03:00",
    period_end: "2026-11-01T00:00:00+03:00",
    seats: 30,
    credits_per_seat: 420,
    pool: 12600,
    used: 3150,
    remaining: 9450,
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

const PACKS: Schemas["CreditPackResponse"][] = [
  { code: "pack_500", credits: 500, price_kopecks: 149_000, valid_months: 12 },
  { code: "pack_2000", credits: 2000, price_kopecks: 549_000, valid_months: 12 },
  { code: "pack_5000", credits: 5000, price_kopecks: 1_299_000, valid_months: 12 },
];

type Order = Schemas["CreditOrderResponse"];

function shell(
  settings = companySettings(),
  month: Schemas["UsageResponse"] = usage(),
): { requests: unknown[]; ordered: unknown[] } {
  const requests: unknown[] = [];
  const ordered: unknown[] = [];
  const orders: Order[] = [];
  server.use(
    http.get("/api/v1/auth/me", () => HttpResponse.json(adminMe())),
    http.get("/api/v1/usage", () => HttpResponse.json(month)),
    http.get("/api/v1/company", () => HttpResponse.json(settings)),
    http.post("/api/v1/company/tariff-request", async ({ request }) => {
      requests.push(await request.json());
      return new HttpResponse(null, { status: 202 });
    }),
    http.get("/api/v1/credits/packs", () => HttpResponse.json(PACKS)),
    http.get("/api/v1/credits/orders", () => HttpResponse.json(orders)),
    http.post("/api/v1/credits/orders", async ({ request }) => {
      const body = (await request.json()) as { pack: string };
      ordered.push(body);
      const pack = PACKS.find((p) => p.code === body.pack) ?? PACKS[0]!;
      const order: Order = {
        id: `o-${orders.length + 1}`,
        number: orders.length + 1,
        pack: pack.code,
        credits: pack.credits,
        amount_kopecks: pack.price_kopecks,
        status: "awaiting_payment",
        payment_method: "invoice",
        created_at: "2026-10-09T10:00:00Z",
        paid_at: null,
        cancelled_at: null,
      };
      orders.unshift(order);
      return HttpResponse.json(order, { status: 201 });
    }),
  );
  return { requests, ordered };
}

describe("тариф", () => {
  it("показывает тариф, места и кредиты; заявка уходит с выбранным тарифом", async () => {
    const user = userEvent.setup();
    const { requests } = shell();
    renderApp("/admin/tariff");

    const plan = await screen.findByRole("region", { name: "Тариф «Базовый»" });
    expect(document.title).toBe("Тариф — kronto");
    expect(within(plan).getByText("990 ₽")).toBeInTheDocument();
    expect(plan).toHaveTextContent("Занято 12 из 30 мест");

    const limit = screen.getByRole("region", { name: "Кредиты" });
    expect(
      within(limit).getByRole("progressbar", { name: "Израсходовано кредитов месячного пула" }),
    ).toHaveAttribute("aria-valuenow", "3150");
    expect(within(limit).getByText("1 октября 2026 г. — 31 октября 2026 г.")).toBeInTheDocument();
    expect(within(limit).getByText("9 450")).toBeInTheDocument();
    expect(within(limit).getByText("осталось в пуле")).toBeInTheDocument();
    expect(limit).toHaveTextContent("Пул обновится 1 ноября 2026 г.");
    expect(limit).toHaveTextContent("Купленных кредитов нет");
    expect(limit).toHaveTextContent(
      "В среднем один вопрос — около 1 кредита; длинные вопросы и ответы списывают больше.",
    );
    expect(screen.getByText(/оплата картой появится позже/)).toBeInTheDocument();

    await user.click(within(plan).getByRole("button", { name: "Сменить тариф" }));
    const dialog = screen.getByRole("dialog", { name: "Сменить тариф" });
    expect(within(dialog).getByRole("radio", { name: /Базовый/ })).toBeChecked();
    expect(within(dialog).getByRole("radio", { name: /Корпоративный/ })).toHaveAccessibleName(
      /Цена по запросу/,
    );
    await user.click(within(dialog).getByRole("radio", { name: /Расширенный/ }));
    await user.type(within(dialog).getByLabelText(/Сколько мест нужно/), "40");
    await user.type(within(dialog).getByLabelText(/Комментарий/), "  Хотим с 1 ноября ");
    await user.click(within(dialog).getByRole("button", { name: "Отправить заявку" }));

    expect(await within(dialog).findByText(/Команда kronto свяжется с вами/)).toBeInTheDocument();
    expect(dialog).toHaveAccessibleName("Заявка отправлена");
    expect(requests).toEqual([{ tariff: "extended", seats: 40, comment: "Хотим с 1 ноября" }]);
    await user.click(within(dialog).getByRole("button", { name: "Готово" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
  });

  it("пустую заявку не шлёт, на лимит заявок отвечает понятно", async () => {
    const user = userEvent.setup();
    let sent = 0;
    shell();
    server.use(
      http.post("/api/v1/company/tariff-request", () => {
        sent += 1;
        return HttpResponse.json(
          { detail: "Слишком много запросов, попробуйте позже" },
          { status: 429 },
        );
      }),
    );
    renderApp("/admin/tariff");

    await user.click(await screen.findByRole("button", { name: "Сменить тариф" }));
    const dialog = screen.getByRole("dialog", { name: "Сменить тариф" });
    await user.click(within(dialog).getByRole("button", { name: "Отправить заявку" }));
    expect(
      within(dialog).getByText("Выберите другой тариф или укажите, сколько мест нужно."),
    ).toBeInTheDocument();
    expect(sent).toBe(0);

    await user.type(within(dialog).getByLabelText(/Сколько мест нужно/), "50");
    await user.click(within(dialog).getByRole("button", { name: "Отправить заявку" }));
    expect(await within(dialog).findByRole("alert")).toHaveTextContent(
      /Заявок на сегодня уже много/,
    );
    expect(sent).toBe(1);
  });

  it("предупреждает, когда места заняты и кредиты кончились; старый адрес ведёт сюда", async () => {
    shell(
      companySettings({ tariff: "extended", members: 30 }),
      usage({ used: 12600, remaining: 0, warning: true, exhausted: true, stopped: true }),
    );
    const { router } = renderApp("/admin/usage");

    const plan = await screen.findByRole("region", { name: "Тариф «Расширенный»" });
    expect(router.state.location.pathname).toBe("/admin/tariff");
    expect(within(plan).getByText("1 490 ₽")).toBeInTheDocument();
    expect(within(plan).getByText(/Свободных мест нет/)).toBeInTheDocument();
    const limit = screen.getByRole("region", { name: "Кредиты" });
    expect(within(limit).getByText("Кредиты закончились")).toBeInTheDocument();
    expect(limit).toHaveTextContent(/не могут задавать вопросы до 1 ноября 2026/);
    expect(limit).toHaveTextContent("Купите пакет кредитов или добавьте места.");
  });

  it("купленные кредиты: остаток и ближайшее сгорание", async () => {
    shell(
      companySettings(),
      usage({
        used: 12600,
        remaining: 0,
        warning: true,
        exhausted: true,
        purchased: 560,
        purchased_expiring: 60,
        purchased_expires_at: "2026-11-20T10:00:00+03:00",
      }),
    );
    renderApp("/admin/tariff");

    const limit = await screen.findByRole("region", { name: "Кредиты" });
    expect(within(limit).getByText("Месячный пул израсходован")).toBeInTheDocument();
    expect(limit).toHaveTextContent("Вопросы списываются с купленных кредитов");
    expect(within(limit).getByText("560")).toBeInTheDocument();
    expect(limit).toHaveTextContent("60 кредитов сгорят 20 ноября 2026 г.");
  });

  it("покупка пакета: цены с бэкенда, заказ ждёт оплаты по счёту", async () => {
    const user = userEvent.setup();
    const { ordered } = shell();
    renderApp("/admin/tariff");

    const packs = await screen.findByRole("region", { name: "Пакеты кредитов" });
    expect(await within(packs).findByText("5 490 ₽")).toBeInTheDocument();
    expect(packs).toHaveTextContent("действуют 12 месяцев");
    expect(within(packs).getByText("Заказов пока нет")).toBeInTheDocument();
    await user.click(within(packs).getByRole("button", { name: /Купить 2\s000 кредитов/ }));

    const dialog = screen.getByRole("dialog", { name: "Купить пакет кредитов" });
    expect(dialog).toHaveTextContent("2 000 кредитов за 5 490 ₽");
    await user.click(within(dialog).getByRole("button", { name: "Заказать" }));

    expect(await within(dialog).findByText(/Заказ № 1 ждёт оплаты/)).toBeInTheDocument();
    expect(ordered).toEqual([{ pack: "pack_2000" }]);
    await user.click(within(dialog).getByRole("button", { name: "Готово" }));
    const table = await within(packs).findByRole("table", { name: "Заказы" });
    expect(within(table).getByText("№ 1")).toBeInTheDocument();
    expect(within(table).getByText("Ждёт оплаты")).toBeInTheDocument();
  });
});
