import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { describe, expect, it } from "vitest";

import type { Schemas } from "../api/client";
import { me } from "../test/fixtures";
import { renderApp } from "../test/render";
import { server } from "../test/server";

type Link = Schemas["SharedLinkResponse"];

const ACTIVE: Link = {
  id: "c-1",
  title: "Отпуск",
  shared_at: "2026-10-04T10:00:00Z",
  expires_at: "2026-11-03T10:00:00Z",
  expired: false,
};
const EXPIRED: Link = {
  id: "c-2",
  title: "Командировки",
  shared_at: "2026-08-20T10:00:00Z",
  expires_at: "2026-09-19T10:00:00Z",
  expired: true,
};

describe("настройки: мои общие ссылки", () => {
  it("срок каждой ссылки; продлить и отключить", async () => {
    const user = userEvent.setup();
    let items = [ACTIVE, EXPIRED];
    const renewed: string[] = [];
    const revoked: string[] = [];
    server.use(
      http.get("/api/v1/auth/me", () => HttpResponse.json(me())),
      http.get("/api/v1/conversations/shares", () => HttpResponse.json({ items })),
      http.post("/api/v1/conversations/:id/share/renew", ({ params }) => {
        const id = String(params.id);
        renewed.push(id);
        items = items.map((item) =>
          item.id === id ? { ...item, expires_at: "2026-11-08T10:00:00Z", expired: false } : item,
        );
        return HttpResponse.json({ token: "tok", ...items.find((item) => item.id === id) });
      }),
      http.delete("/api/v1/conversations/:id/share", ({ params }) => {
        revoked.push(String(params.id));
        items = items.filter((item) => item.id !== params.id);
        return new HttpResponse(null, { status: 204 });
      }),
    );
    renderApp("/settings/shared-links");

    const section = await screen.findByRole("region", { name: "Мои общие ссылки" });
    const table = await within(section).findByRole("table", { name: "Общие ссылки" });
    const rows = within(table).getAllByRole("row").slice(1);
    expect(rows.map((row) => row.textContent)).toEqual([
      expect.stringMatching(/Отпуск.*до 3 ноября 2026/),
      expect.stringMatching(/Командировки.*истекла 19 сентября 2026/),
    ]);
    // Название ведёт в сам диалог: там ссылку копируют.
    expect(within(table).getByRole("link", { name: "Отпуск" })).toHaveAttribute("href", "/c/c-1");

    await user.click(within(table).getByRole("button", { name: "Продлить: Командировки" }));
    expect(await within(table).findByText(/до 8 ноября 2026/)).toBeInTheDocument();
    expect(renewed).toEqual(["c-2"]);

    await user.click(within(table).getByRole("button", { name: "Отключить: Отпуск" }));
    expect(await screen.findByText("Доступ по ссылке закрыт")).toBeInTheDocument();
    expect(revoked).toEqual(["c-1"]);
    expect(within(section).queryByText("Отпуск")).not.toBeInTheDocument();
  });

  it("ссылок нет — подсказка, где их создают", async () => {
    server.use(
      http.get("/api/v1/auth/me", () => HttpResponse.json(me())),
      http.get("/api/v1/conversations/shares", () => HttpResponse.json({ items: [] })),
    );
    renderApp("/settings/shared-links");

    expect(await screen.findByText(/Вы ещё не делились диалогами/)).toBeInTheDocument();
  });
});
