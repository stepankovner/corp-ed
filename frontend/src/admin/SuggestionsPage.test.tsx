import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { describe, expect, it } from "vitest";

import { adminMe } from "../test/fixtures";
import { renderApp } from "../test/render";
import { server } from "../test/server";

type Item = { id: string; text: string };

/** Тексты подсказок в таблице сверху вниз. */
function rows(table: HTMLElement) {
  return within(table)
    .getAllByRole("row")
    .slice(1)
    .map((row) => row.querySelector("td")?.textContent);
}

function row(table: HTMLElement, text: string) {
  const tr = within(table).getByText(text).closest("tr");
  if (!tr) throw new Error("no row");
  return within(tr);
}

/** Сервер подсказок в памяти; bodies — тела запросов на изменение. */
function mockSuggestions(initial: Item[], frequent: string[] = []) {
  let company = initial;
  const bodies: unknown[] = [];
  server.use(
    http.get("/api/v1/auth/me", () => HttpResponse.json(adminMe())),
    http.get("/api/v1/usage", () => HttpResponse.json({ warning: false })),
    http.get("/api/v1/suggestions", () => HttpResponse.json({ company, frequent })),
    http.post("/api/v1/suggestions", async ({ request }) => {
      const body = (await request.json()) as { text: string };
      bodies.push(body);
      const item = { id: `s-${company.length + 1}`, text: body.text };
      company = [...company, item];
      return HttpResponse.json(item, { status: 201 });
    }),
    http.patch("/api/v1/suggestions/:id", async ({ request, params }) => {
      const body = (await request.json()) as { text: string };
      bodies.push(body);
      company = company.map((s) => (s.id === params.id ? { ...s, text: body.text } : s));
      return HttpResponse.json(company.find((s) => s.id === params.id));
    }),
    http.delete("/api/v1/suggestions/:id", ({ params }) => {
      company = company.filter((s) => s.id !== params.id);
      return new HttpResponse(null, { status: 204 });
    }),
    http.put("/api/v1/suggestions/order", async ({ request }) => {
      const body = (await request.json()) as { ids: string[] };
      bodies.push(body);
      company = body.ids.map((id) => company.find((s) => s.id === id) as Item);
      return HttpResponse.json(company);
    }),
  );
  return bodies;
}

describe("подсказки", () => {
  it("показывает подсказки по порядку и частые вопросы", async () => {
    mockSuggestions(
      [
        { id: "s-1", text: "Как оформить отпуск?" },
        { id: "s-2", text: "Где взять справку 2-НДФЛ?" },
      ],
      ["Когда приходит аванс?"],
    );
    renderApp("/admin/suggestions");

    const table = await screen.findByRole("table", { name: "Подсказки" });
    expect(document.title).toBe("Подсказки — kronto");
    expect(rows(table)).toEqual(["Как оформить отпуск?", "Где взять справку 2-НДФЛ?"]);
    expect(screen.getByText("Подсказок: 2 из 12.")).toBeInTheDocument();
    // У краёв списка двигать некуда.
    expect(
      row(table, "Как оформить отпуск?").getByRole("button", { name: "Переместить выше" }),
    ).toBeDisabled();
    expect(
      row(table, "Где взять справку 2-НДФЛ?").getByRole("button", { name: "Переместить ниже" }),
    ).toBeDisabled();

    const frequent = screen.getByRole("region", { name: "Частые вопросы сотрудников" });
    expect(within(frequent).getByText("«Когда приходит аванс?»")).toBeInTheDocument();
  });

  it("без частых вопросов объясняет, когда они появятся", async () => {
    mockSuggestions([]);
    renderApp("/admin/suggestions");

    expect(await screen.findByText("Подсказок пока нет")).toBeInTheDocument();
    const frequent = screen.getByRole("region", { name: "Частые вопросы сотрудников" });
    expect(within(frequent).getByText(/хотя бы трое разных коллег/)).toBeInTheDocument();
  });

  it("добавляет, изменяет и удаляет с подтверждением", async () => {
    const user = userEvent.setup();
    const bodies = mockSuggestions([{ id: "s-1", text: "Как оформить отпуск?" }]);
    renderApp("/admin/suggestions");

    const table = await screen.findByRole("table", { name: "Подсказки" });
    await user.click(screen.getByRole("button", { name: "Добавить подсказку" }));
    await user.type(screen.getByLabelText("Вопрос"), "  Где   взять справку? ");
    await user.click(screen.getByRole("button", { name: "Сохранить" }));
    expect(await within(table).findByText("Где взять справку?")).toBeInTheDocument();

    await user.click(row(table, "Как оформить отпуск?").getByRole("button", { name: "Изменить" }));
    const text = screen.getByLabelText("Вопрос");
    await user.clear(text);
    await user.type(text, "Как уйти в отпуск?");
    await user.click(screen.getByRole("button", { name: "Сохранить" }));
    expect(await within(table).findByText("Как уйти в отпуск?")).toBeInTheDocument();
    expect(bodies).toEqual([{ text: "Где взять справку?" }, { text: "Как уйти в отпуск?" }]);

    await user.click(row(table, "Как уйти в отпуск?").getByRole("button", { name: "Удалить" }));
    const confirm = screen.getByRole("dialog", { name: "Удалить подсказку?" });
    expect(within(confirm).getByText(/пропадёт с пустого экрана чата/)).toBeInTheDocument();
    await user.click(within(confirm).getByRole("button", { name: "Удалить" }));
    await screen.findByText("Подсказок: 1 из 12.");
    expect(within(table).queryByText("Как уйти в отпуск?")).not.toBeInTheDocument();
  });

  it("переставляет подсказку ниже", async () => {
    const user = userEvent.setup();
    const bodies = mockSuggestions([
      { id: "s-1", text: "Первая" },
      { id: "s-2", text: "Вторая" },
      { id: "s-3", text: "Третья" },
    ]);
    renderApp("/admin/suggestions");

    const table = await screen.findByRole("table", { name: "Подсказки" });
    await user.click(row(table, "Вторая").getByRole("button", { name: "Переместить ниже" }));
    await waitFor(() => expect(rows(table)).toEqual(["Первая", "Третья", "Вторая"]));
    expect(bodies).toEqual([{ ids: ["s-1", "s-3", "s-2"] }]);
    // Строка дошла до конца: «ниже» выключена, фокус — на соседней кнопке.
    const moved = row(table, "Вторая");
    expect(moved.getByRole("button", { name: "Переместить ниже" })).toBeDisabled();
    expect(moved.getByRole("button", { name: "Переместить выше" })).toHaveFocus();
  });

  it("при двенадцати подсказках новую добавить нельзя", async () => {
    mockSuggestions(Array.from({ length: 12 }, (_, i) => ({ id: `s-${i}`, text: `Вопрос ${i}` })));
    renderApp("/admin/suggestions");

    await screen.findByRole("table", { name: "Подсказки" });
    const add = screen.getByRole("button", { name: "Добавить подсказку" });
    expect(add).toBeDisabled();
    expect(add).toHaveAccessibleDescription(/Добавлено 12 из 12/);
  });
});
