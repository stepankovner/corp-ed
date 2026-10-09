import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { describe, expect, it } from "vitest";

import type { Schemas } from "../api/client";
import { adminMe, me } from "../test/fixtures";
import { renderApp } from "../test/render";
import { server } from "../test/server";

type Settings = Schemas["NotificationSettingsResponse"];

/** Профиль и настройки писем, как их хранит сервер; PUT — только изменённые флаги. */
function signedInAs(
  profile: Schemas["MeResponse"],
  { failPut = false }: { failPut?: boolean } = {},
) {
  let settings: Settings = {
    email_connectors: true,
    email_credits: true,
    email_join_requests: true,
    email_weekly_digest: false,
  };
  let reads = 0;
  const puts: unknown[] = [];
  server.use(
    http.get("/api/v1/auth/me", () => HttpResponse.json(profile)),
    http.get("/api/v1/usage", () => HttpResponse.json({ warning: false })),
    http.get("/api/v1/notifications", () => HttpResponse.json({ items: [], unread: 0 })),
    http.get("/api/v1/notifications/settings", () => {
      reads += 1;
      return HttpResponse.json(settings);
    }),
    http.put("/api/v1/notifications/settings", async ({ request }) => {
      const body = (await request.json()) as Partial<Settings>;
      puts.push(body);
      if (failPut) return HttpResponse.json({ detail: "Сервер недоступен" }, { status: 503 });
      settings = { ...settings, ...body };
      return HttpResponse.json(settings);
    }),
  );
  return { puts, reads: () => reads };
}

describe("настройки: уведомления", () => {
  it("администратор включает и выключает письма — PUT с одним флагом", async () => {
    const user = userEvent.setup();
    const { puts } = signedInAs(adminMe());
    renderApp("/settings/notifications");

    const emails = await screen.findByRole("region", { name: "Письма администратору" });
    expect(document.title).toBe("Уведомления — kronto");
    const connectors = await within(emails).findByRole("switch", {
      name: "Остановилось подключение",
    });
    expect(connectors).toBeChecked();
    expect(connectors).toHaveAccessibleDescription(
      "Источник перестал отдавать документы — нужно ввести доступ заново.",
    );
    expect(within(emails).getByRole("switch", { name: "Кредиты" })).toBeChecked();
    expect(within(emails).getByRole("switch", { name: "Заявки" })).toBeChecked();
    const digest = within(emails).getByRole("switch", { name: "Недельная сводка" });
    expect(digest).not.toBeChecked();
    expect(digest).toHaveAccessibleDescription(
      "По понедельникам: вопросы, частые темы, пробелы в документах.",
    );

    await user.click(digest);
    expect(digest).toBeChecked();
    expect(await screen.findByText("Сохранено")).toBeInTheDocument();
    await user.click(connectors);
    expect(connectors).not.toBeChecked();
    await waitFor(() =>
      expect(puts).toEqual([{ email_weekly_digest: true }, { email_connectors: false }]),
    );
    expect(digest).toBeChecked();
    expect(connectors).not.toBeChecked();

    // Письма о безопасности — всем, без переключателей.
    const security = screen.getByRole("region", { name: "Письма о безопасности" });
    expect(within(security).getByText("вход с нового устройства;")).toBeInTheDocument();
    expect(within(security).queryByRole("switch")).not.toBeInTheDocument();
  });

  it("быстрые щелчки уходят по очереди, последний — побеждает", async () => {
    const user = userEvent.setup();
    const { puts } = signedInAs(adminMe());
    renderApp("/settings/notifications");

    const credits = await screen.findByRole("switch", { name: "Кредиты" });
    await user.click(credits);
    await user.click(screen.getByRole("switch", { name: "Заявки" }));
    await user.click(credits);

    await waitFor(() =>
      expect(puts).toEqual([
        { email_credits: false },
        { email_join_requests: false },
        { email_credits: true },
      ]),
    );
    await waitFor(() => expect(screen.getAllByText("Сохранено").length).toBeGreaterThan(0));
    expect(credits).toBeChecked();
    expect(screen.getByRole("switch", { name: "Заявки" })).not.toBeChecked();
  });

  it("сервер не сохранил — переключатель возвращается, причина в сообщении", async () => {
    const user = userEvent.setup();
    signedInAs(adminMe(), { failPut: true });
    renderApp("/settings/notifications");

    const credits = await screen.findByRole("switch", { name: "Кредиты" });
    await user.click(credits);
    expect(await screen.findByText("Сервер недоступен")).toBeInTheDocument();
    expect(credits).toBeChecked();
  });

  it("сотрудник видит только письма о безопасности, настройки не запрашиваются", async () => {
    const { reads } = signedInAs(me());
    renderApp("/settings/notifications");

    const security = await screen.findByRole("region", { name: "Письма о безопасности" });
    expect(document.title).toBe("Уведомления — kronto");
    expect(within(security).getByText("всегда включены")).toBeInTheDocument();
    expect(
      screen.getByText(/Письма об остановленных подключениях.*получают администраторы компании/),
    ).toBeInTheDocument();
    expect(screen.queryByRole("region", { name: "Письма администратору" })).not.toBeInTheDocument();
    expect(screen.queryByRole("switch")).not.toBeInTheDocument();
    expect(reads()).toBe(0);
  });
});
