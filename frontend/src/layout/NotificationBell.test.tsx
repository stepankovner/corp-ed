import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { Schemas } from "../api/client";
import { adminMe, loneMe, me } from "../test/fixtures";
import { renderApp } from "../test/render";
import { server } from "../test/server";

type Item = Schemas["NotificationResponse"];

const MINUTE = 60_000;

function note(overrides: Partial<Item> = {}): Item {
  return {
    id: "n-1",
    kind: "connector_stopped",
    title: "Остановилось подключение «Битрикс24»",
    body: "Источник перестал отдавать документы.\nВведите доступ заново.",
    link: "/settings/connections",
    created_at: new Date(Date.now() - 5 * MINUTE).toISOString(),
    read: false,
    ...overrides,
  };
}

/** Профиль, лента уведомлений и отметки «прочитано», как их видит сервер. */
function signedInAs(profile: Schemas["MeResponse"], initial: Item[] = []) {
  let items = initial;
  let gets = 0;
  const reads: unknown[] = [];
  const state = () => ({ items, unread: items.filter((item) => !item.read).length });
  server.use(
    http.get("/api/v1/auth/me", () => HttpResponse.json(profile)),
    http.get("/api/v1/usage", () => HttpResponse.json({ warning: false })),
    http.get("/api/v1/account/company-requests", () => HttpResponse.json([])),
    http.get("/api/v1/notifications", () => {
      gets += 1;
      return HttpResponse.json(state());
    }),
    http.post("/api/v1/notifications/read", async ({ request }) => {
      const body = (await request.json()) as { ids?: string[] };
      reads.push(body);
      items = items.map((item) =>
        !body.ids || body.ids.includes(item.id) ? { ...item, read: true } : item,
      );
      return HttpResponse.json(state());
    }),
  );
  return { reads, gets: () => gets };
}

function three(): Item[] {
  return [
    note(),
    note({
      id: "n-2",
      kind: "credits_warning",
      title: "Израсходовано 80 % лимита вопросов",
      body: "Осталось 200 вопросов до 1 ноября.",
      link: "/admin/tariff",
      created_at: new Date(Date.now() - 3 * 60 * MINUTE).toISOString(),
    }),
    note({
      id: "n-3",
      kind: "join_request",
      title: "Заявка на вступление: Пётр Иванов",
      body: "petr@meridian-stroy.ru",
      link: null,
    }),
    note({
      id: "n-4",
      kind: "weekly_digest",
      title: "Сводка за неделю",
      body: "Вопросов: 42",
      link: null,
      read: true,
    }),
  ];
}

afterEach(() => {
  vi.useRealTimers();
});

describe("колокольчик", () => {
  it("число непрочитанных — на значке и в имени кнопки", async () => {
    signedInAs(me(), three());
    renderApp("/");

    const bell = await screen.findByRole("button", { name: "Уведомления: 3 непрочитанных" });
    expect(within(bell).getByText("3")).toBeInTheDocument();
  });

  it("больше девяти — «9+», одно — «непрочитанное»", async () => {
    const many = Array.from({ length: 12 }, (_, index) => note({ id: `n-${index}` }));
    signedInAs(me(), many);
    renderApp("/");
    const bell = await screen.findByRole("button", { name: "Уведомления: 12 непрочитанных" });
    expect(within(bell).getByText("9+")).toBeInTheDocument();
  });

  it("всё прочитано — без числа", async () => {
    signedInAs(me(), [note({ read: true })]);
    renderApp("/");
    const bell = await screen.findByRole("button", { name: "Уведомления" });
    expect(bell).toHaveTextContent("");
  });

  it("панель: пункты по порядку, время, «Прочитать все» и настройка писем", async () => {
    const user = userEvent.setup();
    signedInAs(adminMe(), three());
    renderApp("/");

    await user.click(await screen.findByRole("button", { name: /^Уведомления: 3/ }));
    const menu = await screen.findByRole("menu");
    const items = within(menu).getAllByRole("menuitem");
    expect(items.map((item) => item.textContent.trim())).toEqual([
      "Прочитать все",
      expect.stringContaining("Остановилось подключение «Битрикс24»"),
      expect.stringContaining("Израсходовано 80 % лимита вопросов"),
      expect.stringContaining("Заявка на вступление: Пётр Иванов"),
      expect.stringContaining("Сводка за неделю"),
      "Настроить письма",
    ]);
    // Непрочитанные скринридер называет «Новое», прочитанные — нет.
    const connector = within(menu).getByRole("menuitem", {
      name: "Новое: Остановилось подключение «Битрикс24»",
    });
    expect(connector).toHaveAttribute("href", "/settings/connections");
    expect(connector).toHaveAccessibleDescription(
      "Источник перестал отдавать документы.\nВведите доступ заново. 5 минут назад",
    );
    expect(within(menu).getByRole("menuitem", { name: "Сводка за неделю" })).toBeInTheDocument();
    expect(within(menu).getByRole("menuitem", { name: "Настроить письма" })).toHaveAttribute(
      "href",
      "/settings/notifications",
    );
  });

  it("щелчок по пункту отмечает его прочитанным и ведёт по ссылке", async () => {
    const user = userEvent.setup();
    const { reads } = signedInAs(me(), three());
    const { router } = renderApp("/");

    await user.click(await screen.findByRole("button", { name: /^Уведомления: 3/ }));
    await user.click(await screen.findByRole("menuitem", { name: /Остановилось подключение/ }));

    await waitFor(() => expect(router.state.location.pathname).toBe("/settings/connections"));
    expect(reads).toEqual([{ ids: ["n-1"] }]);
    expect(
      await screen.findByRole("button", { name: "Уведомления: 2 непрочитанных" }),
    ).toBeInTheDocument();
    expect(screen.queryByRole("menu")).not.toBeInTheDocument();

    // Прочитанный пункт без ссылки — повторно не отмечается.
    await user.click(screen.getByRole("button", { name: /^Уведомления: 2/ }));
    await user.click(await screen.findByRole("menuitem", { name: "Сводка за неделю" }));
    expect(router.state.location.pathname).toBe("/settings/connections");
    expect(reads).toEqual([{ ids: ["n-1"] }]);
  });

  it("«Прочитать все» — без ids, панель остаётся открытой", async () => {
    const user = userEvent.setup();
    const { reads } = signedInAs(me(), three());
    renderApp("/");

    await user.click(await screen.findByRole("button", { name: /^Уведомления: 3/ }));
    const menu = await screen.findByRole("menu");
    await user.click(within(menu).getByRole("menuitem", { name: "Прочитать все" }));

    await waitFor(() => expect(reads).toEqual([{}]));
    // Пока меню открыто, остальная страница для скринридера скрыта (Radix).
    expect(
      await screen.findByRole("button", { name: "Уведомления", hidden: true }),
    ).toBeInTheDocument();
    expect(screen.getByRole("menu")).toBe(menu);
    expect(within(menu).queryByRole("menuitem", { name: "Прочитать все" })).not.toBeInTheDocument();
    expect(within(menu).queryByRole("menuitem", { name: /^Новое:/ })).not.toBeInTheDocument();
    expect(menu).toHaveFocus();
  });

  it("пусто: сотруднику — коротко, администратору — что здесь появится", async () => {
    const user = userEvent.setup();
    signedInAs(me());
    renderApp("/");
    await user.click(await screen.findByRole("button", { name: "Уведомления" }));
    const menu = await screen.findByRole("menu");
    expect(within(menu).getByText("Пока ничего нет")).toBeInTheDocument();
    expect(within(menu).queryByRole("menuitem")).not.toBeInTheDocument();
  });

  it("пусто у администратора — подсказка и ссылка на письма", async () => {
    const user = userEvent.setup();
    signedInAs(adminMe());
    renderApp("/");
    await user.click(await screen.findByRole("button", { name: "Уведомления" }));
    const menu = await screen.findByRole("menu");
    expect(
      within(menu).getByText(
        "Пока ничего — здесь появятся остановленные подключения, кредиты и заявки на вступление",
      ),
    ).toBeInTheDocument();
    expect(
      within(menu)
        .getAllByRole("menuitem")
        .map((item) => item.textContent.trim()),
    ).toEqual(["Настроить письма"]);
  });

  it("опрашивает сервер раз в минуту", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const { gets } = signedInAs(me(), three());
    renderApp("/");
    await screen.findByRole("button", { name: /^Уведомления: 3/ });
    expect(gets()).toBe(1);

    await vi.advanceTimersByTimeAsync(MINUTE);
    await waitFor(() => expect(gets()).toBe(2));
  });

  it("сервер отказал (403) — колокольчик прячется и не опрашивает", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    let gets = 0;
    signedInAs(me());
    server.use(
      http.get("/api/v1/notifications", () => {
        gets += 1;
        return HttpResponse.json({ detail: "Нет компании", code: "no_company" }, { status: 403 });
      }),
    );
    renderApp("/");
    await waitFor(() => expect(gets).toBe(1));
    await waitFor(() =>
      expect(screen.queryByRole("button", { name: /^Уведомления/ })).not.toBeInTheDocument(),
    );
    await vi.advanceTimersByTimeAsync(2 * MINUTE);
    expect(gets).toBe(1);
  });

  it.each([
    ["без компании", loneMe()],
    ["без надёжного входа", adminMe({ mfa: { strong: false, strong_required: true } })],
  ])("%s колокольчика нет и запроса тоже", async (_, profile) => {
    const { gets } = signedInAs(profile);
    renderApp("/settings/companies");
    expect(await screen.findByRole("heading", { name: "Ваши компании" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /^Уведомления/ })).not.toBeInTheDocument();
    expect(gets()).toBe(0);
  });
});
