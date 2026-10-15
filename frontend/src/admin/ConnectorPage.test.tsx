import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { describe, expect, it } from "vitest";

import type { Schemas } from "../api/client";
import { adminMe } from "../test/fixtures";
import { renderApp } from "../test/render";
import { server } from "../test/server";

type Connector = Schemas["ConnectorResponse"];
type Kind = Schemas["ConnectorKindResponse"];

const TOKEN = "kag_0b1c2d3e-aaaa-bbbb-cccc-000000000001_0b1c2d3e-aaaa-bbbb-cccc-000000000002_xyz";

function kind(overrides: Partial<Kind> = {}): Kind {
  return {
    kind: "dokuwiki",
    title: "DokuWiki",
    mode: "organization",
    modules: [{ name: "pages", title: "Страницы" }],
    config_fields: [
      {
        name: "base_url",
        title: "Адрес DokuWiki",
        required: true,
        secret: false,
        multiline: false,
      },
      {
        name: "users",
        title: "Сотрудники: «логин почта» по одному на строку",
        required: true,
        secret: false,
        multiline: true,
      },
    ],
    credential_fields: [
      {
        name: "token",
        title: "Токен суперпользователя",
        required: false,
        secret: true,
        multiline: false,
      },
    ],
    app_credential_fields: [],
    oauth: false,
    oauth_callback_url: null,
    extra: {},
    base: true,
    push: false,
    available: true,
    limit_reached: false,
    ...overrides,
  };
}

function connector(overrides: Partial<Connector> = {}): Connector {
  return {
    id: "c-1",
    kind: "dokuwiki",
    name: "Вики компании",
    mode: "organization",
    status: "active",
    config: {
      base_url: "https://wiki.meridian-stroy.ru/",
      users: "ivanov ivanov@meridian-stroy.ru",
    },
    modules: ["pages"],
    credentials_set_at: "2026-09-01T09:00:00Z",
    last_error_code: null,
    last_sync_at: null,
    sync_interval_minutes: 60,
    grants_active: null,
    members_active: null,
    created_at: "2026-09-01T09:00:00Z",
    updated_at: "2026-09-01T09:00:00Z",
    ...overrides,
  };
}

/** Администратор на странице подключения; PATCH и POST ключа записываются. */
function mockConnector(row: Connector, kinds: Kind[]) {
  const calls: { method: string; path: string; body: unknown }[] = [];
  server.use(
    http.get("/api/v1/auth/me", () => HttpResponse.json(adminMe())),
    http.get("/api/v1/usage", () => HttpResponse.json({ warning: false })),
    http.get("/api/v1/connectors/kinds", () => HttpResponse.json(kinds)),
    http.get("/api/v1/connectors/:id", () => HttpResponse.json(row)),
    http.get("/api/v1/connectors/:id/runs", () => HttpResponse.json([])),
    http.patch("/api/v1/connectors/:id", async ({ request }) => {
      const body: unknown = await request.json();
      calls.push({ method: "PATCH", path: "/connectors/c-1", body });
      return HttpResponse.json(row);
    }),
    http.post("/api/v1/connectors/:id/agent-token", () => {
      calls.push({ method: "POST", path: "/connectors/c-1/agent-token", body: null });
      return HttpResponse.json({ token: TOKEN });
    }),
  );
  return calls;
}

describe("страница подключения", () => {
  it("список по строкам — textarea, переводы строк уходят на сервер", async () => {
    const user = userEvent.setup();
    const calls = mockConnector(connector(), [kind()]);
    renderApp("/admin/sources/connections/c-1");

    const users = await screen.findByLabelText(/Сотрудники: «логин почта»/);
    expect(users.tagName).toBe("TEXTAREA");
    expect(users).toHaveValue("ivanov ivanov@meridian-stroy.ru");
    // Обычное поле остаётся однострочным.
    expect(screen.getByLabelText("Адрес DokuWiki").tagName).toBe("INPUT");

    await user.type(users, "{Enter}petrov petrov@meridian-stroy.ru");
    await user.click(screen.getByRole("button", { name: "Сохранить настройки" }));

    await waitFor(() => expect(calls).toHaveLength(1));
    expect(calls[0]?.body).toMatchObject({
      config: { users: "ivanov ivanov@meridian-stroy.ru\npetrov petrov@meridian-stroy.ru" },
    });
  });

  it("вид с агентом: вместо учётных данных — кнопка ключа, ключ виден один раз", async () => {
    const user = userEvent.setup();
    const push = kind({
      kind: "smb_agent",
      title: "Сетевые папки (агент в сети компании)",
      config_fields: [],
      credential_fields: [],
      push: true,
    });
    const calls = mockConnector(
      connector({ kind: "smb_agent", name: "Файловый сервер", config: {} }),
      [push],
    );
    renderApp("/admin/sources/connections/c-1");

    const button = await screen.findByRole("button", { name: "Выпустить ключ агента" });
    expect(screen.queryByRole("button", { name: "Сохранить ключи" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Синхронизировать" })).not.toBeInTheDocument();

    await user.click(button);
    const dialog = await screen.findByRole("dialog", { name: "Выпустить ключ агента?" });
    expect(dialog).toHaveTextContent("Прежний ключ перестанет действовать");
    expect(calls).toHaveLength(0);

    await user.click(within(dialog).getByRole("button", { name: "Выпустить ключ" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(calls).toEqual([{ method: "POST", path: "/connectors/c-1/agent-token", body: null }]);

    expect(screen.getByTestId("agent-token").textContent).toBe(TOKEN);
    expect(screen.getByText(/Инструкция по установке — у команды kronto/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Скопировать ключ" }));
    expect(await navigator.clipboard.readText()).toBe(TOKEN);

    // «Я сохранил ключ» — ключ скрыт и больше не показывается.
    await user.click(screen.getByRole("button", { name: "Я сохранил ключ" }));
    expect(screen.queryByTestId("agent-token")).not.toBeInTheDocument();
  });

  it("обычный вид: поля учётных данных, кнопки ключа агента нет", async () => {
    mockConnector(connector(), [kind()]);
    renderApp("/admin/sources/connections/c-1");

    expect(await screen.findByLabelText(/Токен суперпользователя/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Выпустить ключ агента" })).not.toBeInTheDocument();
  });
});
