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

function shell(
  settings = companySettings(),
  month: Schemas["UsageResponse"] = usage(),
): { requests: unknown[] } {
  const requests: unknown[] = [];
  server.use(
    http.get("/api/v1/auth/me", () => HttpResponse.json(adminMe())),
    http.get("/api/v1/usage", () => HttpResponse.json(month)),
    http.get("/api/v1/company", () => HttpResponse.json(settings)),
    http.post("/api/v1/company/tariff-request", async ({ request }) => {
      requests.push(await request.json());
      return new HttpResponse(null, { status: 202 });
    }),
  );
  return { requests };
}

describe("тариф", () => {
  it("показывает тариф, места и лимит; заявка уходит с выбранным тарифом", async () => {
    const user = userEvent.setup();
    const { requests } = shell();
    renderApp("/admin/tariff");

    const plan = await screen.findByRole("region", { name: "Тариф «Базовый»" });
    expect(document.title).toBe("Тариф — kronto");
    expect(within(plan).getByText("990 ₽")).toBeInTheDocument();
    expect(plan).toHaveTextContent("Занято 12 из 30 мест");

    const limit = screen.getByRole("region", { name: "Лимит вопросов" });
    expect(
      within(limit).getByRole("progressbar", { name: "Израсходовано кредитов" }),
    ).toHaveAttribute("aria-valuenow", "3150");
    expect(within(limit).getByText("1 октября 2026 г. — 31 октября 2026 г.")).toBeInTheDocument();
    expect(within(limit).getByText("9 450")).toBeInTheDocument();
    expect(within(limit).getByText("осталось кредитов")).toBeInTheDocument();
    expect(screen.getByText(/Оплата картой и по счёту появится позже/)).toBeInTheDocument();

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

  it("предупреждает, когда места заняты и лимит исчерпан; старый адрес ведёт сюда", async () => {
    shell(
      companySettings({ tariff: "extended", members: 30 }),
      usage({ used: 12600, remaining: 0, warning: true, exhausted: true }),
    );
    const { router } = renderApp("/admin/usage");

    const plan = await screen.findByRole("region", { name: "Тариф «Расширенный»" });
    expect(router.state.location.pathname).toBe("/admin/tariff");
    expect(within(plan).getByText("1 490 ₽")).toBeInTheDocument();
    expect(within(plan).getByText(/Свободных мест нет/)).toBeInTheDocument();
    const limit = screen.getByRole("region", { name: "Лимит вопросов" });
    expect(within(limit).getByText("Лимит исчерпан")).toBeInTheDocument();
    expect(
      within(limit).getByText(/не могут задавать вопросы до 1 ноября 2026/),
    ).toBeInTheDocument();
  });
});
