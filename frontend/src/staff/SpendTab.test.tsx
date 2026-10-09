import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { describe, expect, it } from "vitest";

import type { Schemas } from "../api/client";
import { adminMe } from "../test/fixtures";
import { renderApp } from "../test/render";
import { server } from "../test/server";

type Spend = Schemas["SpendResponse"];

function spend(overrides: Partial<Spend> = {}): Spend {
  return {
    since: "2026-09-05",
    until: "2026-10-04",
    questions: 40,
    input_tokens: 120000,
    output_tokens: 30000,
    credits: 52,
    rub: 75,
    rub_per_1k_tokens: 0.5,
    days: [
      { day: "2026-10-03", questions: 15, tokens: 50000, credits: 20 },
      { day: "2026-10-04", questions: 25, tokens: 100000, credits: 32 },
    ],
    models: [{ model: "gpt-4o-mini", questions: 40, input_tokens: 120000, output_tokens: 30000 }],
    companies: [
      {
        tenant_id: "t-1",
        ref: "1a2b3c4d",
        name: "ООО «Меридиан Строй»",
        company_code: "meridian",
        questions: 30,
        tokens: 112500,
        credits: 40,
      },
      {
        tenant_id: "t-2",
        ref: "9f00aa11",
        name: "АО «Север»",
        company_code: "sever",
        questions: 10,
        tokens: 37500,
        credits: 12,
      },
    ],
    ...overrides,
  };
}

function mockSpend(make: (days: number) => Spend) {
  const asked: number[] = [];
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
    http.get("/api/v1/staff/spend", ({ request }) => {
      const days = Number(new URL(request.url).searchParams.get("days"));
      asked.push(days);
      return HttpResponse.json(make(days));
    }),
  );
  return asked;
}

describe("расход на модель", () => {
  it("показывает вопросы, токены, кредиты и рубли; период меняет запрос", async () => {
    const asked = mockSpend((days) =>
      days === 7
        ? spend({ questions: 5, input_tokens: 9000, output_tokens: 1000, credits: 6, rub: 5 })
        : spend(),
    );
    const user = userEvent.setup();
    renderApp("/staff/spend");

    expect(await screen.findByText("≈ 75 ₽")).toBeInTheDocument();
    expect(document.title).toBe("Расход — kronto");
    expect(screen.getByText("150 000")).toBeInTheDocument();
    expect(screen.getByText("вход 120 000 / выход 30 000")).toBeInTheDocument();
    expect(screen.getByText("52")).toBeInTheDocument();
    expect(screen.getByText("0,5 ₽ за 1 000 токенов")).toBeInTheDocument();
    expect(screen.queryByText(/цена не задана/)).not.toBeInTheDocument();

    const days = screen.getByRole("table", { name: "Токены по дням" });
    expect(within(days).getAllByRole("row")).toHaveLength(3);

    const models = screen.getByRole("table", { name: "Расход по моделям" });
    expect(within(models).getByText("gpt-4o-mini")).toBeInTheDocument();

    const companies = screen.getByRole("table", { name: "Расход по компаниям" });
    const meridian = within(companies).getByText("ООО «Меридиан Строй»").closest("tr")!;
    expect(within(meridian).getByText("meridian · 1a2b3c4d")).toBeInTheDocument();
    expect(within(meridian).getByText("75 %")).toBeInTheDocument();
    expect(within(meridian).getByText("56,25")).toBeInTheDocument();
    const sever = within(companies).getByText("АО «Север»").closest("tr")!;
    expect(within(sever).getByText("25 %")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "7 дней" }));
    expect(await screen.findByText("≈ 5 ₽")).toBeInTheDocument();
    expect(screen.getByText("вход 9 000 / выход 1 000")).toBeInTheDocument();
    expect(asked).toEqual([30, 7]);
  });

  it("без цены — прочерк и подсказка; без вопросов — пустое состояние", async () => {
    mockSpend(() =>
      spend({
        questions: 0,
        input_tokens: 0,
        output_tokens: 0,
        credits: 0,
        rub: null,
        rub_per_1k_tokens: null,
        days: [{ day: "2026-10-04", questions: 0, tokens: 0, credits: 0 }],
        models: [],
        companies: [],
      }),
    );
    renderApp("/staff/spend");

    expect(await screen.findByText("Вопросов не было")).toBeInTheDocument();
    expect(screen.getByText("—")).toBeInTheDocument();
    expect(screen.getByText(/цена не задана/)).toHaveTextContent(
      "цена не задана: BILLING_LLM_RUB_PER_1K_TOKENS",
    );
    expect(screen.queryByText(/₽/)).not.toBeInTheDocument();
    expect(screen.queryByRole("table")).not.toBeInTheDocument();
  });
});
