import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { describe, expect, it } from "vitest";

import type { Schemas } from "../api/client";
import { loneMe, me } from "../test/fixtures";
import { renderApp } from "../test/render";
import { server } from "../test/server";

describe("настройки: вкладки", () => {
  it("/settings ведёт на профиль, вкладки — ссылки с отметкой текущей", async () => {
    const user = userEvent.setup();
    server.use(
      http.get("/api/v1/auth/me", () => HttpResponse.json(me())),
      http.get("/api/v1/account/company-requests", () => HttpResponse.json([])),
    );
    const { router } = renderApp("/settings");

    expect(await screen.findByRole("heading", { name: "Имя и фамилия" })).toBeInTheDocument();
    expect(router.state.location.pathname).toBe("/settings/profile");
    expect(document.title).toBe("Профиль — kronto");
    const tabs = screen.getByRole("navigation", { name: "Разделы настроек" });
    expect(
      within(tabs)
        .getAllByRole("link")
        .map((link) => link.textContent),
    ).toEqual(["Профиль", "Безопасность", "Компании", "Управление учётной записью"]);
    expect(within(tabs).getByRole("link", { name: "Профиль" })).toHaveAttribute(
      "aria-current",
      "page",
    );

    await user.click(within(tabs).getByRole("link", { name: "Компании" }));
    expect(router.state.location.pathname).toBe("/settings/companies");
    expect(await screen.findByRole("heading", { name: "Ваши компании" })).toBeInTheDocument();
    expect(within(tabs).getByRole("link", { name: "Компании" })).toHaveAttribute(
      "aria-current",
      "page",
    );
    expect(within(tabs).getByRole("link", { name: "Профиль" })).not.toHaveAttribute("aria-current");
    expect(document.title).toBe("Компании — kronto");
  });

  it("незнакомая вкладка — на профиль; настройки открываются и без компании", async () => {
    server.use(http.get("/api/v1/auth/me", () => HttpResponse.json(loneMe())));
    const { router } = renderApp("/settings/notifications");

    expect(await screen.findByRole("heading", { name: "Настройки" })).toBeInTheDocument();
    await waitFor(() => expect(router.state.location.pathname).toBe("/settings/profile"));
    // Уведомления и «Мои подключения» — следующими этапами, заглушек нет.
    expect(screen.queryByRole("link", { name: "Уведомления" })).not.toBeInTheDocument();
  });
});

describe("настройки: профиль", () => {
  it("сохраняет имя и фамилию без лишних пробелов и перечитывает профиль", async () => {
    const user = userEvent.setup();
    let profile = me();
    let body: unknown;
    let reads = 0;
    server.use(
      http.get("/api/v1/auth/me", () => {
        reads += 1;
        return HttpResponse.json(profile);
      }),
      http.patch("/api/v1/account", async ({ request }) => {
        body = await request.json();
        const next = body as Schemas["NameUpdateRequest"];
        profile = me({
          first_name: next.first_name,
          last_name: next.last_name,
          full_name: `${next.first_name} ${next.last_name}`,
        });
        return new HttpResponse(null, { status: 204 });
      }),
    );
    renderApp("/settings/profile");

    const first = await screen.findByLabelText("Имя");
    expect(first).toHaveValue("Анна");
    const email = screen.getByRole("region", { name: "Почта" });
    expect(within(email).getByText("anna@meridian-stroy.ru")).toBeInTheDocument();
    expect(
      within(email).getByRole("link", { name: "«Управление учётной записью»" }),
    ).toHaveAttribute("href", "/settings/account");
    const save = screen.getByRole("button", { name: "Сохранить" });
    expect(save).toBeDisabled();

    const last = screen.getByLabelText("Фамилия");
    await user.clear(last);
    await user.type(last, "  Петрова  ");
    const before = reads;
    await user.click(save);

    expect(await screen.findByText("Имя сохранено")).toBeInTheDocument();
    expect(body).toEqual({ first_name: "Анна", last_name: "Петрова" });
    expect(reads).toBeGreaterThan(before);
    // Новое имя — и в учётной записи в боковой панели.
    expect(
      await screen.findByRole("button", { name: /^Профиль: Анна Петрова/ }),
    ).toBeInTheDocument();
  });

  it("не отправляет пустую фамилию", async () => {
    const user = userEvent.setup();
    let sent = false;
    server.use(
      http.get("/api/v1/auth/me", () => HttpResponse.json(me())),
      http.patch("/api/v1/account", () => {
        sent = true;
        return new HttpResponse(null, { status: 204 });
      }),
    );
    renderApp("/settings/profile");

    await user.clear(await screen.findByLabelText("Фамилия"));
    await user.type(screen.getByLabelText("Фамилия"), "   ");
    await user.click(screen.getByRole("button", { name: "Сохранить" }));

    expect(await screen.findByText("Укажите фамилию.")).toBeInTheDocument();
    expect(sent).toBe(false);
  });
});
