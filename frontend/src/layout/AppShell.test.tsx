import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { Schemas } from "../api/client";
import { getSession } from "../api/session";
import { setThemePreference } from "../lib/theme";
import { adminMe, me } from "../test/fixtures";
import { renderApp } from "../test/render";
import { server } from "../test/server";

function signedInAs(profile: Schemas["MeResponse"] = me()) {
  server.use(
    http.get("/api/v1/auth/me", () => HttpResponse.json(profile)),
    http.get("/api/v1/usage", () => HttpResponse.json({ warning: false })),
    http.get("/api/v1/connectors/mine", () => HttpResponse.json([])),
    http.get("/api/v1/materials", () => HttpResponse.json([])),
    http.get("/api/v1/users", () => HttpResponse.json([])),
    http.get("/api/v1/invites", () => HttpResponse.json([])),
    http.get("/api/v1/people", () => HttpResponse.json([])),
    http.get("/api/v1/departments", () => HttpResponse.json([])),
  );
}

/** Телефон: matchMedia, по которой оболочка уводит панель в выдвижное меню. */
function onPhone() {
  window.matchMedia = vi.fn((query: string) => ({
    matches: query.includes("max-width"),
    media: query,
    addEventListener: () => undefined,
    removeEventListener: () => undefined,
  })) as unknown as typeof window.matchMedia;
}

afterEach(() => {
  vi.restoreAllMocks();
  // @ts-expect-error -- в jsdom matchMedia нет, возвращаем как было.
  delete window.matchMedia;
  setThemePreference("system");
});

describe("боковая панель", () => {
  it("показывает компанию, разделы и учётную запись", async () => {
    signedInAs(me({ full_name: "Анна Смирнова" }));
    renderApp("/");
    await screen.findByRole("log", { name: "Переписка" });

    expect(screen.getByRole("button", { name: /^Компания: ООО «Меридиан Строй»/ })).toBeVisible();
    const nav = screen.getByRole("navigation", { name: "Разделы" });
    expect(within(nav).getByRole("link", { name: "Коллеги" })).not.toHaveAttribute("aria-current");
    // Диалоги — списком под разделами (ТЗ §6).
    expect(screen.getByRole("region", { name: "Диалоги" })).toBeVisible();
    expect(await screen.findByText("Здесь появятся ваши диалоги")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /^Профиль: Анна Смирнова/ })).toBeVisible();
    expect(document.title).toBe("Вопросы — kronto");
  });

  it("показывает логотип компании вместо инициалов", async () => {
    const logo = "/api/v1/logos/t-1?v=abc&exp=1&sig=def";
    signedInAs(
      me({
        companies: [
          {
            tenant_id: "t-1",
            company_name: "ООО «Меридиан Строй»",
            role: "employee",
            status: "active",
            logo_url: logo,
          },
        ],
      }),
    );
    renderApp("/");
    const switcher = await screen.findByRole("button", { name: /^Компания: ООО «Меридиан Строй»/ });
    expect(switcher.querySelector("img")).toHaveAttribute("src", logo);
  });

  it("команде kronto — «Панель kronto», остальных туда не пускает", async () => {
    signedInAs(adminMe({ staff: true }));
    server.use(
      http.get("/api/v1/staff/overview", () =>
        HttpResponse.json({
          companies: 3,
          active_companies: 2,
          pilots_ending: 1,
          requests_new: 2,
          accounts: 40,
        }),
      ),
      http.get("/api/v1/staff/requests", () => HttpResponse.json([])),
    );
    const { router } = renderApp("/");
    const nav = await screen.findByRole("navigation", { name: "Разделы" });
    await userEvent.setup().click(within(nav).getByRole("link", { name: "Панель kronto" }));
    expect(await screen.findByRole("heading", { name: "Панель kronto" })).toBeInTheDocument();
    await waitFor(() => expect(router.state.location.pathname).toBe("/staff/requests"));
    expect(await screen.findByText(/новых заявок: 2/)).toBeInTheDocument();
  });

  it("не из команды — «Панели kronto» нет, адрес ведёт на главную", async () => {
    signedInAs(adminMe());
    const { router } = renderApp("/staff/companies");
    await screen.findByRole("log", { name: "Переписка" });
    expect(router.state.location.pathname).toBe("/");
    expect(screen.queryByRole("link", { name: "Панель kronto" })).not.toBeInTheDocument();
  });

  it("команда без приложения или ключа — сначала защита входа", async () => {
    signedInAs(me({ staff: true }));
    const { router } = renderApp("/staff");
    await waitFor(() => expect(router.state.location.pathname).toBe("/settings/security"));
  });

  it("у администратора разделы управления раскрыты в панели, второй панели нет", async () => {
    const user = userEvent.setup();
    signedInAs(adminMe());
    renderApp("/admin/users");
    expect(await screen.findByRole("heading", { name: "Сотрудники" })).toBeInTheDocument();
    expect(document.title).toBe("Сотрудники — kronto");

    const sections = screen.getByRole("list", { name: "Управление" });
    expect(within(sections).getByRole("link", { name: "Сотрудники" })).toHaveAttribute(
      "aria-current",
      "page",
    );
    expect(within(sections).getAllByRole("link")).toHaveLength(10);
    expect(screen.getAllByRole("navigation")).toHaveLength(1);

    await user.click(screen.getByRole("button", { name: "Свернуть разделы управления" }));
    expect(screen.queryByRole("list", { name: "Управление" })).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Показать разделы управления" }));
    expect(screen.getByRole("list", { name: "Управление" })).toBeInTheDocument();
  });

  it("«Новый диалог» ведёт к пустому экрану вопросов", async () => {
    const user = userEvent.setup();
    signedInAs();
    const { router } = renderApp("/people");
    await user.click(await screen.findByRole("button", { name: "Новый диалог" }));
    await screen.findByRole("log", { name: "Переписка" });
    expect(router.state.location.pathname).toBe("/");
    expect(screen.getByRole("heading", { name: "Здравствуйте, Анна!" })).toBeInTheDocument();
  });

  it("сворачивается до значков и помнит это", async () => {
    const user = userEvent.setup();
    signedInAs();
    const first = renderApp("/");
    await user.click(await screen.findByRole("button", { name: "Свернуть панель" }));
    expect(screen.getByRole("button", { name: "Развернуть панель" })).toHaveAttribute(
      "aria-expanded",
      "false",
    );
    expect(localStorage.getItem("kronto.sidebar")).toBe("collapsed");
    // Подписи остаются для скринридера.
    expect(screen.getByRole("link", { name: "Коллеги" })).toBeInTheDocument();
    first.unmount();

    renderApp("/");
    expect(await screen.findByRole("button", { name: "Развернуть панель" })).toBeInTheDocument();
  });

  it("сворачивается и без доступа к хранилищу", async () => {
    const user = userEvent.setup();
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new DOMException("denied", "SecurityError");
    });
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new DOMException("denied", "SecurityError");
    });
    signedInAs();
    renderApp("/");
    await user.click(await screen.findByRole("button", { name: "Свернуть панель" }));
    expect(screen.getByRole("button", { name: "Развернуть панель" })).toBeInTheDocument();
  });
});

describe("меню учётной записи", () => {
  it("меняет тему и выходит", async () => {
    const user = userEvent.setup();
    signedInAs(me({ full_name: "Анна Смирнова" }));
    server.use(http.post("/api/v1/auth/logout", () => new HttpResponse(null, { status: 204 })));
    renderApp("/");
    await user.click(await screen.findByRole("button", { name: /^Профиль/ }));
    const menu = await screen.findByRole("menu");
    expect(within(menu).getByRole("menuitem", { name: "Настройки" })).toHaveAttribute(
      "href",
      "/settings",
    );
    expect(within(menu).getByRole("group", { name: "Тема" })).toBeInTheDocument();
    expect(within(menu).getByRole("menuitemradio", { name: "Системная" })).toHaveAttribute(
      "aria-checked",
      "true",
    );

    await user.click(within(menu).getByRole("menuitemradio", { name: "Тёмная" }));
    expect(document.documentElement.dataset.theme).toBe("dark");
    expect(localStorage.getItem("kronto.theme")).toBe("dark");
    // Меню осталось открытым: видно, что выбор применился.
    expect(within(menu).getByRole("menuitemradio", { name: "Тёмная" })).toHaveAttribute(
      "aria-checked",
      "true",
    );

    await user.click(within(menu).getByRole("menuitem", { name: "Выйти" }));
    expect(await screen.findByLabelText("Почта")).toBeInTheDocument();
    expect(getSession()).toBeNull();
  });
});

describe("телефон", () => {
  it("панель — выдвижное меню: Esc закрывает, фокус возвращается к кнопке", async () => {
    const user = userEvent.setup();
    onPhone();
    signedInAs();
    renderApp("/");
    const burger = await screen.findByRole("button", { name: "Открыть меню" });
    expect(burger).toHaveAttribute("aria-expanded", "false");
    // Закрытое меню недоступно: ни для фокуса, ни для скринридера.
    expect(document.getElementById("app-sidebar")).toHaveAttribute("inert");

    await user.click(burger);
    const drawer = screen.getByRole("dialog", { name: "Меню" });
    expect(drawer).not.toHaveAttribute("inert");
    expect(within(drawer).getByRole("button", { name: "Закрыть меню" })).toHaveFocus();

    await user.keyboard("{Escape}");
    expect(screen.queryByRole("dialog", { name: "Меню" })).not.toBeInTheDocument();
    await waitFor(() => expect(burger).toHaveFocus());
  });

  it("меню закрывается при переходе по разделу", async () => {
    const user = userEvent.setup();
    onPhone();
    signedInAs();
    const { router } = renderApp("/");
    await user.click(await screen.findByRole("button", { name: "Открыть меню" }));
    await user.click(
      within(screen.getByRole("dialog", { name: "Меню" })).getByRole("link", {
        name: "Коллеги",
      }),
    );
    expect(router.state.location.pathname).toBe("/people");
    expect(screen.queryByRole("dialog", { name: "Меню" })).not.toBeInTheDocument();
  });

  it("Tab не уходит из открытого меню", async () => {
    const user = userEvent.setup();
    onPhone();
    signedInAs();
    renderApp("/");
    await user.click(await screen.findByRole("button", { name: "Открыть меню" }));
    const close = screen.getByRole("button", { name: "Закрыть меню" });
    await user.tab({ shift: true });
    expect(screen.getByRole("dialog", { name: "Меню" })).toContainElement(
      document.activeElement as HTMLElement,
    );
    expect(document.activeElement).not.toBe(close);
    await user.tab();
    expect(close).toHaveFocus();
  });
});

describe("заголовок вкладки", () => {
  it("на входе и на несуществующей странице", async () => {
    renderApp("/login", { signedIn: false });
    await screen.findByLabelText("Почта");
    expect(document.title).toBe("Вход — kronto");
  });

  it("на странице, которой нет", async () => {
    signedInAs();
    renderApp("/no-such-page");
    expect(await screen.findByRole("heading", { name: "Такой страницы нет" })).toBeVisible();
    expect(document.title).toBe("Страница не найдена — kronto");
  });
});
