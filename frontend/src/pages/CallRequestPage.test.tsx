import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { describe, expect, it } from "vitest";

import type { Schemas } from "../api/client";
import { renderApp } from "../test/render";
import { server } from "../test/server";

function form(overrides: Partial<Schemas["LeadFormResponse"]> = {}): Schemas["LeadFormResponse"] {
  return {
    enabled: true,
    policy_url: "https://kronto.example/privacy",
    policy_version: "2026-09-28",
    slots: ["10:00–12:00", "12:00–14:00", "14:00–16:00", "16:00–18:00"],
    first_date: "2026-09-29",
    last_date: "2026-10-28",
    timezone: "Europe/Moscow",
    ...overrides,
  };
}

describe("тарифы и запись на созвон", () => {
  it("тарифы открыты без входа: три тарифа с ценами", async () => {
    renderApp("/pricing", { signedIn: false });

    expect(await screen.findByRole("heading", { name: "Тарифы" })).toBeInTheDocument();
    const base = screen.getByRole("region", { name: "Базовый" });
    expect(within(base).getByText("990 ₽")).toBeInTheDocument();
    expect(within(base).getByText(/До 20 обращений в день на место/)).toBeInTheDocument();
    expect(within(base).getByText(/до 5 подключений/)).toBeInTheDocument();
    expect(within(base).getByRole("link", { name: "Записаться на созвон" })).toHaveAttribute(
      "href",
      "/pricing/request?tariff=base",
    );
    const extended = screen.getByRole("region", { name: "Расширенный" });
    expect(within(extended).getByText("1 290 ₽")).toBeInTheDocument();
    const enterprise = screen.getByRole("region", { name: "Корпоративный" });
    expect(within(enterprise).getByText("По запросу")).toBeInTheDocument();
    expect(within(enterprise).getByRole("link", { name: "Обсудить на созвоне" })).toHaveAttribute(
      "href",
      "/pricing/request?tariff=enterprise",
    );
    // Бесплатный первый месяц команда предлагает сама — на сайте его нет.
    expect(screen.queryByText(/Пилот/)).not.toBeInTheDocument();
  });

  it.each([
    ["/privacy", "/privacy"],
    ["https://krontoai.ru/privacy", "https://krontoai.ru/privacy"],
    ["javascript:alert(1)", "#"],
    ["data:text/html,x", "#"],
  ])("ссылка на политику %s — только свой путь или http(s)", async (policyUrl, href) => {
    server.use(
      http.get("/api/v1/leads/form", () => HttpResponse.json(form({ policy_url: policyUrl }))),
    );
    renderApp("/pricing/request", { signedIn: false });

    expect(await screen.findByRole("link", { name: "политике обработки данных" })).toHaveAttribute(
      "href",
      href,
    );
  });

  it("пока политика не задана, форма закрыта", async () => {
    server.use(
      http.get("/api/v1/leads/form", () =>
        HttpResponse.json(form({ enabled: false, policy_url: null, policy_version: null })),
      ),
    );
    renderApp("/pricing/request", { signedIn: false });

    expect(await screen.findByText("Запись скоро откроется")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Отправить заявку" })).not.toBeInTheDocument();
  });

  it("заявка уходит с согласием и версией политики", async () => {
    const user = userEvent.setup();
    let body: unknown;
    server.use(
      http.get("/api/v1/leads/form", () => HttpResponse.json(form())),
      http.post("/api/v1/leads", async ({ request }) => {
        body = await request.json();
        return HttpResponse.json({ status: "received" }, { status: 201 });
      }),
    );
    renderApp("/pricing/request?tariff=enterprise", { signedIn: false });

    await user.type(await screen.findByLabelText("Компания"), "ООО «Меридиан Строй»");
    await user.type(screen.getByLabelText("Сколько сотрудников работают за компьютером"), "60");
    await user.type(screen.getByLabelText("Как к вам обращаться"), "Анна");
    await user.type(screen.getByLabelText("Телефон"), "+7 999 123-45-67");
    await user.type(screen.getByLabelText("Удобная дата"), "2026-10-01");
    await user.selectOptions(screen.getByLabelText("Удобное время (по Москве)"), "14:00–16:00");
    expect(screen.getByRole("link", { name: "политике обработки данных" })).toHaveAttribute(
      "href",
      "https://kronto.example/privacy",
    );
    const submit = screen.getByRole("button", { name: "Отправить заявку" });
    expect(submit).toBeDisabled();
    await user.click(screen.getByRole("checkbox", { name: /Согласен на обработку/ }));
    await user.click(submit);

    expect(await screen.findByText("Заявка отправлена")).toBeInTheDocument();
    expect(body).toEqual({
      company_name: "ООО «Меридиан Строй»",
      contact_name: "Анна",
      phone: "+7 999 123-45-67",
      email: null,
      seats: 60,
      tariff: "enterprise",
      preferred_date: "2026-10-01",
      preferred_slot: "14:00–16:00",
      comment: null,
      policy_version: "2026-09-28",
      consent: true,
      website: "",
    });
  });

  it("со страницы входа есть путь к тарифам", async () => {
    renderApp("/login", { signedIn: false });

    expect(await screen.findByRole("link", { name: "Тарифы и запись на созвон" })).toHaveAttribute(
      "href",
      "/pricing",
    );
  });
});
