import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { describe, expect, it } from "vitest";

import type { Schemas } from "../api/client";
import { adminMe } from "../test/fixtures";
import { renderApp } from "../test/render";
import { server } from "../test/server";

type Lead = Schemas["StaffLeadResponse"];

function lead(overrides: Partial<Lead> = {}): Lead {
  return {
    id: "l-1",
    company_name: "ООО «Ромашка»",
    contact_name: "Ирина Петрова",
    phone: "+7 (900) 123-45-67",
    email: "irina@romashka.ru",
    seats: 25,
    tariff: "extended",
    preferred_date: "2026-10-07",
    preferred_slot: "14:00–16:00",
    comment: "Хотим попробовать на отделе кадров",
    status: "new",
    created_at: "2026-10-03T10:00:00Z",
    ...overrides,
  };
}

function shell(initial: Lead[]) {
  let leads = initial;
  const asked: (string | null)[] = [];
  const patches: { id: string; body: unknown }[] = [];
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
    http.get("/api/v1/staff/leads", ({ request }) => {
      const status = new URL(request.url).searchParams.get("status");
      asked.push(status);
      return HttpResponse.json(status ? leads.filter((item) => item.status === status) : leads);
    }),
    http.patch("/api/v1/staff/leads/:leadId", async ({ params, request }) => {
      const body = (await request.json()) as { status: Lead["status"] };
      patches.push({ id: String(params.leadId), body });
      leads = leads.map((item) =>
        item.id === params.leadId ? { ...item, status: body.status } : item,
      );
      return HttpResponse.json(leads.find((item) => item.id === params.leadId));
    }),
  );
  return { asked, patches };
}

describe("заявки на созвон", () => {
  it("показывает новые заявки с контактами; фильтр уходит в запрос", async () => {
    const user = userEvent.setup();
    const { asked } = shell([
      lead(),
      lead({
        id: "l-2",
        company_name: "АО «Север»",
        email: null,
        comment: null,
        tariff: "enterprise",
        seats: 1,
        status: "scheduled",
      }),
    ]);
    renderApp("/staff/leads");

    const card = await screen.findByRole("listitem", { name: "ООО «Ромашка»" });
    expect(document.title).toBe("Созвоны — kronto");
    expect(screen.queryByRole("listitem", { name: "АО «Север»" })).not.toBeInTheDocument();
    expect(asked).toEqual(["new"]);

    expect(within(card).getByText("Ирина Петрова")).toBeInTheDocument();
    expect(within(card).getByRole("link", { name: "+7 (900) 123-45-67" })).toHaveAttribute(
      "href",
      "tel:+79001234567",
    );
    expect(within(card).getByRole("link", { name: "irina@romashka.ru" })).toHaveAttribute(
      "href",
      "mailto:irina@romashka.ru",
    );
    expect(within(card).getByText("«Расширенный», 25 мест")).toBeInTheDocument();
    expect(within(card).getByText("7 октября 2026 г., 14:00–16:00 по Москве")).toBeInTheDocument();
    expect(within(card).getByText("Хотим попробовать на отделе кадров")).toBeInTheDocument();
    expect(within(card).getByText("новая")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Назначен" }));
    const sever = await screen.findByRole("listitem", { name: "АО «Север»" });
    expect(within(sever).getByText("«Корпоративный», 1 место")).toBeInTheDocument();
    expect(within(sever).queryByRole("link", { name: /@/ })).not.toBeInTheDocument();
    expect(screen.queryByRole("listitem", { name: "ООО «Ромашка»" })).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Все" }));
    expect(await screen.findByRole("listitem", { name: "ООО «Ромашка»" })).toBeInTheDocument();
    expect(asked).toEqual(["new", "scheduled", null]);

    await user.click(screen.getByRole("button", { name: "Отклонены" }));
    expect(await screen.findByText("Отклонённых заявок нет")).toBeInTheDocument();
  });

  it("статус меняется списком: PATCH со статусом и сообщение", async () => {
    const user = userEvent.setup();
    const { patches } = shell([lead()]);
    renderApp("/staff/leads");

    const card = await screen.findByRole("listitem", { name: "ООО «Ромашка»" });
    await user.selectOptions(
      within(card).getByRole("combobox", { name: "Статус заявки: ООО «Ромашка»" }),
      "contacted",
    );

    expect(await screen.findByText("ООО «Ромашка» — перезвонили")).toBeInTheDocument();
    expect(patches).toEqual([{ id: "l-1", body: { status: "contacted" } }]);
    // В «Новых» её больше нет.
    await waitFor(() =>
      expect(screen.queryByRole("listitem", { name: "ООО «Ромашка»" })).not.toBeInTheDocument(),
    );
    expect(screen.getByText("Новых заявок нет")).toBeInTheDocument();
  });
});
