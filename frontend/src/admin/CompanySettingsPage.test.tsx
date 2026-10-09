import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { describe, expect, it, vi } from "vitest";

import { api, type Schemas } from "../api/client";
import { adminMe } from "../test/fixtures";
import { renderApp } from "../test/render";
import { server } from "../test/server";

type Settings = Schemas["CompanySettingsResponse"];

function companySettings(overrides: Partial<Settings> = {}): Settings {
  return {
    id: "t-1",
    name: "ООО «Меридиан Строй»",
    company_code: "meridian",
    logo_url: null,
    not_found_mode: "general",
    mfa_policy: "any",
    allow_remember_device: true,
    email_domains: ["meridian-stroy.ru"],
    tariff: "base",
    seats: 30,
    members: 12,
    daily_credits_per_member: null,
    ...overrides,
  };
}

/** Оболочка администратора: профиль и лимит для плашки. */
function shell() {
  let meRequests = 0;
  server.use(
    http.get("/api/v1/auth/me", () => {
      meRequests += 1;
      return HttpResponse.json(adminMe());
    }),
    http.get("/api/v1/usage", () => HttpResponse.json({ warning: false })),
  );
  return { meRequests: () => meRequests };
}

describe("настройки компании", () => {
  it("меняет название, режим ответа, защиту входа и домены — только изменённое", async () => {
    const user = userEvent.setup();
    const me = shell();
    let settings = companySettings();
    const bodies: Partial<Settings>[] = [];
    server.use(
      http.get("/api/v1/company", () => HttpResponse.json(settings)),
      http.patch("/api/v1/company", async ({ request }) => {
        const body = (await request.json()) as Partial<Settings>;
        bodies.push(body);
        settings = { ...settings, ...body };
        return HttpResponse.json(settings);
      }),
    );
    renderApp("/admin/settings");

    const company = await screen.findByRole("region", { name: "Компания" });
    expect(document.title).toBe("Настройки компании — kronto");
    expect(within(company).getByText("meridian")).toBeInTheDocument();

    // Название: пробелы чистим, а переключатель компаний перечитывает /auth/me.
    const name = within(company).getByLabelText("Название");
    expect(name).toHaveValue("ООО «Меридиан Строй»");
    const save = within(company).getByRole("button", { name: "Сохранить" });
    expect(save).toBeDisabled();
    await user.clear(name);
    await user.type(name, "  Меридиан   Строй ");
    const before = me.meRequests();
    await user.click(save);
    expect(await screen.findByText("Название сохранено")).toBeInTheDocument();
    expect(me.meRequests()).toBeGreaterThan(before);

    // Режим «ответа нет» — сразу, без кнопки.
    expect(screen.getByRole("radio", { name: /Общий ответ с пометкой/ })).toBeChecked();
    await user.click(screen.getByRole("radio", { name: /Честный отказ/ }));
    await waitFor(() => expect(bodies).toHaveLength(2));
    expect(screen.getByRole("radio", { name: /Честный отказ/ })).toBeChecked();

    // Строгая защита запирает сотрудников без приложения — сначала спрашиваем.
    const strong = screen.getByRole("switch", { name: /Требовать приложение или ключ доступа/ });
    expect(strong).toHaveAccessibleDescription(/Администраторам это обязательно всегда/);
    await user.click(strong);
    const ask = screen.getByRole("dialog", { name: "Требовать приложение или ключ доступа?" });
    await user.click(within(ask).getByRole("button", { name: "Отмена" }));
    expect(bodies).toHaveLength(2);
    expect(strong).not.toBeChecked();
    await user.click(strong);
    await user.click(
      within(
        screen.getByRole("dialog", { name: "Требовать приложение или ключ доступа?" }),
      ).getByRole("button", { name: "Требовать" }),
    );
    await waitFor(() => expect(strong).toBeChecked());
    expect(
      screen.queryByRole("dialog", { name: "Требовать приложение или ключ доступа?" }),
    ).not.toBeInTheDocument();

    const remember = screen.getByRole("switch", { name: /Запомнить это устройство/ });
    expect(remember).toBeChecked();
    await user.click(remember);
    await waitFor(() => expect(remember).not.toBeChecked());
    expect(await screen.findByText("Теперь второй шаг — при каждом входе")).toBeInTheDocument();

    // Домены: Enter добавляет плашку, крестик убирает, «Сохранить» отправляет
    // весь список — и введённый, но ещё не добавленный домен тоже.
    const domains = screen.getByRole("region", { name: "Домены почты" });
    const field = within(domains).getByLabelText("Добавить домен");
    await user.type(field, "https://Meridian-SPB.ru/{Enter}");
    const list = within(domains).getByRole("list", { name: "Домены почты" });
    expect(within(list).getByText("meridian-spb.ru")).toBeInTheDocument();
    expect(field).toHaveValue("");
    await user.click(within(domains).getByRole("button", { name: "Убрать meridian-stroy.ru" }));
    expect(within(list).queryByText("meridian-stroy.ru")).not.toBeInTheDocument();
    expect(field).toHaveFocus();
    await user.type(field, "anna@MS-Group.ru");
    await user.click(within(domains).getByRole("button", { name: "Сохранить" }));
    expect(await screen.findByText("Домены сохранены")).toBeInTheDocument();

    expect(bodies).toEqual([
      { name: "Меридиан Строй" },
      { not_found_mode: "strict" },
      { mfa_policy: "strong" },
      { allow_remember_device: false },
      { email_domains: ["meridian-spb.ru", "ms-group.ru"] },
    ]);
  });

  it("дневной лимит кредитов: по умолчанию выключен, задаётся и снимается", async () => {
    const user = userEvent.setup();
    shell();
    let settings = companySettings();
    const bodies: Partial<Settings>[] = [];
    server.use(
      http.get("/api/v1/company", () => HttpResponse.json(settings)),
      http.patch("/api/v1/company", async ({ request }) => {
        const body = (await request.json()) as Partial<Settings>;
        bodies.push(body);
        settings = { ...settings, ...body };
        return HttpResponse.json(settings);
      }),
    );
    renderApp("/admin/settings");

    const section = await screen.findByRole("region", { name: "Личный дневной лимит" });
    expect(section).toHaveTextContent("Сейчас лимита нет");
    const field = within(section).getByLabelText(/Кредитов в день на сотрудника/);
    expect(field).toHaveValue(null);
    await user.type(field, "0");
    await user.click(within(section).getByRole("button", { name: "Сохранить" }));
    expect(within(section).getByText(/целое число от 1/)).toBeInTheDocument();
    expect(bodies).toEqual([]);

    await user.clear(field);
    await user.type(field, "15");
    await user.click(within(section).getByRole("button", { name: "Сохранить" }));
    expect(await screen.findByText("Дневной лимит сохранён")).toBeInTheDocument();
    // После сохранения раздел начинается с сохранённого значения.
    const saved = screen.getByRole("region", { name: "Личный дневной лимит" });
    expect(saved).toHaveTextContent("15 кредитов в день");

    await user.click(within(saved).getByRole("button", { name: "Снять лимит" }));
    expect(await screen.findByText("Дневной лимит снят")).toBeInTheDocument();
    expect(bodies).toEqual([{ daily_credits_per_member: 15 }, { daily_credits_per_member: null }]);
  });

  it("домены: явную опечатку ловит сразу, ошибку сервера показывает под полем", async () => {
    const user = userEvent.setup();
    shell();
    let sent = 0;
    server.use(
      http.get("/api/v1/company", () => HttpResponse.json(companySettings({ email_domains: [] }))),
      http.patch("/api/v1/company", () => {
        sent += 1;
        return HttpResponse.json(
          { detail: "Не похоже на домен почты: acme-.ru", code: "invalid_domain" },
          { status: 400 },
        );
      }),
    );
    renderApp("/admin/settings");

    const domains = await screen.findByRole("region", { name: "Домены почты" });
    expect(within(domains).getByText(/Доменов нет — вступить можно с любой почтой/)).toBeVisible();
    const field = within(domains).getByLabelText("Добавить домен");
    await user.type(field, "acme{Enter}");
    expect(field).toHaveAccessibleDescription("Не похоже на домен почты: acme");
    expect(field).toBeInvalid();

    await user.clear(field);
    await user.type(field, "acme-.ru{Enter}");
    await user.click(within(domains).getByRole("button", { name: "Сохранить" }));
    await waitFor(() =>
      expect(field).toHaveAccessibleDescription("Не похоже на домен почты: acme-.ru"),
    );
    expect(sent).toBe(1);
  });

  it("логотип: загрузка, отказ сервера, проверка размера и удаление", async () => {
    const user = userEvent.setup({ applyAccept: false });
    const me = shell();
    let settings = companySettings();
    const logo = "/api/v1/logos/t-1?v=1&exp=1&sig=x";
    // FormData из jsdom fetch из Node не принимает — отправку файла проверяет
    // e2e в браузере. Здесь — что уходит в api.PUT и что происходит потом.
    const put = vi
      .spyOn(api, "PUT")
      .mockImplementationOnce(async () => {
        settings = { ...settings, logo_url: logo };
        return {
          data: { logo_url: logo },
          response: new Response(null, { status: 200 }),
        } as never;
      })
      .mockImplementationOnce(
        async () =>
          ({
            error: {
              detail: "Загрузите логотип в PNG, JPEG или WebP до 5 МБ",
              code: "invalid_logo",
            },
            response: new Response(null, { status: 400 }),
          }) as never,
      );
    server.use(
      http.get("/api/v1/company", () => HttpResponse.json(settings)),
      http.delete("/api/v1/company/logo", () => {
        settings = { ...settings, logo_url: null };
        return new HttpResponse(null, { status: 204 });
      }),
    );
    const { container } = renderApp("/admin/settings");

    const company = await screen.findByRole("region", { name: "Компания" });
    expect(within(company).queryByRole("img", { name: "Логотип компании" })).toBeNull();
    const input = container.querySelector<HTMLInputElement>('input[type="file"]');
    if (!input) throw new Error("no file input");
    expect(input).toHaveAttribute("accept", "image/png,image/jpeg,image/webp");

    const file = new File(["png"], "logo.png", { type: "image/png" });
    const before = me.meRequests();
    await user.upload(input, file);
    expect(await screen.findByText("Логотип обновлён")).toBeInTheDocument();
    expect(put).toHaveBeenCalledWith(
      "/api/v1/company/logo",
      expect.objectContaining({ body: { file } }),
    );
    expect(within(company).getByRole("img", { name: "Логотип компании" })).toHaveAttribute(
      "src",
      logo,
    );
    expect(me.meRequests()).toBeGreaterThan(before);
    expect(within(company).getByRole("button", { name: /Заменить логотип/ })).toBeInTheDocument();

    await user.upload(input, new File(["?"], "broken.png", { type: "image/png" }));
    expect(
      await within(company).findByText("Загрузите логотип в PNG, JPEG или WebP до 5 МБ"),
    ).toBeInTheDocument();

    await user.upload(input, new File(["gif"], "logo.gif", { type: "image/gif" }));
    expect(within(company).getByText("Подойдёт картинка в PNG, JPEG или WebP.")).toBeVisible();
    const big = new File([new Uint8Array(5 * 1024 * 1024 + 1)], "big.png", { type: "image/png" });
    await user.upload(input, big);
    expect(within(company).getByText("Файл больше 5 МБ — выберите поменьше.")).toBeVisible();
    expect(put).toHaveBeenCalledTimes(2);

    await user.click(within(company).getByRole("button", { name: /Удалить логотип/ }));
    expect(await screen.findByText("Логотип удалён")).toBeInTheDocument();
    await waitFor(() =>
      expect(within(company).queryByRole("img", { name: "Логотип компании" })).toBeNull(),
    );
    expect(within(company).getByRole("button", { name: /Загрузить логотип/ })).toBeInTheDocument();
    put.mockRestore();
  });
});
