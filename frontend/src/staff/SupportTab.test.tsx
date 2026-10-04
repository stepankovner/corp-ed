import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { describe, expect, it } from "vitest";

import type { Schemas } from "../api/client";
import { adminMe } from "../test/fixtures";
import { renderApp } from "../test/render";
import { server } from "../test/server";

type Request = Schemas["StaffSupportResponse"];

function support(overrides: Partial<Request> = {}): Request {
  return {
    id: "s-1",
    topic: "login",
    message: "Не приходит код на почту.\nПроверила спам — пусто.",
    status: "new",
    created_at: "2026-10-03T10:00:00Z",
    updated_at: "2026-10-03T10:00:00Z",
    email: "irina@romashka.ru",
    name: "Ирина Петрова",
    company: "ООО «Ромашка»",
    ...overrides,
  };
}

function shell(initial: Request[]) {
  let requests = initial;
  const asked: (string | null)[] = [];
  const patches: { id: string; body: unknown }[] = [];
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
    http.get("/api/v1/staff/support", ({ request }) => {
      const status = new URL(request.url).searchParams.get("status");
      asked.push(status);
      return HttpResponse.json(
        status ? requests.filter((item) => item.status === status) : requests,
      );
    }),
    http.patch("/api/v1/staff/support/:requestId", async ({ params, request }) => {
      const body = (await request.json()) as { status: Request["status"] };
      patches.push({ id: String(params.requestId), body });
      requests = requests.map((item) =>
        item.id === params.requestId ? { ...item, status: body.status } : item,
      );
      return HttpResponse.json(requests.find((item) => item.id === params.requestId));
    }),
  );
  return { asked, patches };
}

describe("обращения", () => {
  it("показывает новые обращения с почтой и текстом; фильтр уходит в запрос", async () => {
    const user = userEvent.setup();
    const { asked } = shell([
      support(),
      support({
        id: "s-2",
        topic: "billing",
        message: "Сколько стоит расширенный тариф?",
        status: "answered",
        name: null,
        email: "boss@sever.ru",
        company: null,
      }),
    ]);
    renderApp("/staff/support");

    const card = await screen.findByRole("listitem", { name: "Ирина Петрова" });
    expect(document.title).toBe("Обращения — kronto");
    expect(asked).toEqual(["new"]);
    expect(screen.queryByRole("listitem", { name: "boss@sever.ru" })).not.toBeInTheDocument();

    expect(within(card).getByText(/^вход и учётная запись · /)).toBeInTheDocument();
    expect(within(card).getByRole("link", { name: "irina@romashka.ru" })).toHaveAttribute(
      "href",
      `mailto:irina@romashka.ru?subject=${encodeURIComponent("kronto: ваше обращение")}`,
    );
    expect(within(card).getByText("ООО «Ромашка»")).toBeInTheDocument();
    // Переносы строк — как написал человек.
    const message = within(card).getByText(/Не приходит код на почту/);
    expect(message.textContent).toBe("Не приходит код на почту.\nПроверила спам — пусто.");
    expect(message).toHaveStyle({ whiteSpace: "pre-wrap" });
    expect(within(card).getByText("новое")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Отвечены" }));
    const sever = await screen.findByRole("listitem", { name: "boss@sever.ru" });
    expect(within(sever).getByText(/^тариф и оплата · /)).toBeInTheDocument();
    expect(within(sever).getByText("без компании")).toBeInTheDocument();
    expect(within(sever).getByText("отвечено")).toBeInTheDocument();
    expect(screen.queryByRole("listitem", { name: "Ирина Петрова" })).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Все" }));
    expect(await screen.findByRole("listitem", { name: "Ирина Петрова" })).toBeInTheDocument();
    expect(screen.getByRole("listitem", { name: "boss@sever.ru" })).toBeInTheDocument();
    expect(asked).toEqual(["new", "answered", null]);

    await user.click(screen.getByRole("button", { name: "Закрыты" }));
    expect(await screen.findByText("Закрытых обращений нет")).toBeInTheDocument();
    expect(asked).toEqual(["new", "answered", null, "closed"]);
  });

  it("статус меняется списком: PATCH со статусом и сообщение", async () => {
    const user = userEvent.setup();
    const { patches } = shell([support()]);
    renderApp("/staff/support");

    const card = await screen.findByRole("listitem", { name: "Ирина Петрова" });
    await user.selectOptions(
      within(card).getByRole("combobox", { name: "Статус обращения: Ирина Петрова" }),
      "answered",
    );

    expect(await screen.findByText("Ирина Петрова — отвечено")).toBeInTheDocument();
    expect(patches).toEqual([{ id: "s-1", body: { status: "answered" } }]);
    // В «Новых» его больше нет.
    await waitFor(() =>
      expect(screen.queryByRole("listitem", { name: "Ирина Петрова" })).not.toBeInTheDocument(),
    );
    expect(screen.getByText("Новых обращений нет")).toBeInTheDocument();
  });

  it("без имени и почты — «Без имени», ссылки нет", async () => {
    shell([support({ name: null, email: null })]);
    renderApp("/staff/support");
    const card = await screen.findByRole("listitem", { name: "Без имени" });
    expect(within(card).queryByRole("link")).not.toBeInTheDocument();
    expect(within(card).getByText("не указана")).toBeInTheDocument();
  });
});
