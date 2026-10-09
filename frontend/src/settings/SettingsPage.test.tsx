import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { describe, expect, it, vi } from "vitest";

import { api, type Schemas } from "../api/client";
import { loneMe, me } from "../test/fixtures";
import { renderApp } from "../test/render";
import { server } from "../test/server";

describe("настройки: вкладки", () => {
  it("/settings ведёт на профиль, вкладки — ссылки с отметкой текущей", async () => {
    const user = userEvent.setup();
    server.use(
      http.get("/api/v1/auth/me", () => HttpResponse.json(me())),
      http.get("/api/v1/account/company-requests", () => HttpResponse.json([])),
      http.get("/api/v1/departments", () => HttpResponse.json([])),
    );
    const { router } = renderApp("/settings");

    expect(await screen.findByRole("heading", { name: "Личные данные" })).toBeInTheDocument();
    expect(router.state.location.pathname).toBe("/settings/profile");
    expect(document.title).toBe("Профиль — kronto");
    const tabs = screen.getByRole("navigation", { name: "Разделы настроек" });
    expect(
      within(tabs)
        .getAllByRole("link")
        .map((link) => link.textContent),
    ).toEqual([
      "Профиль",
      "Безопасность",
      "Мои подключения",
      "Уведомления",
      "Общие ссылки",
      "Компании",
      "Управление учётной записью",
    ]);
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
    // «Мои подключения» и «Уведомления» — только в компании.
    expect(screen.queryByRole("link", { name: "Уведомления" })).not.toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Мои подключения" })).not.toBeInTheDocument();
    // Без компании нет и раздела о работе в ней.
    expect(screen.queryByRole("heading", { name: /^Работа в/ })).not.toBeInTheDocument();
  });
});

describe("настройки: профиль", () => {
  function personal() {
    return screen.getByRole("region", { name: "Личные данные" });
  }

  it("сохраняет имя, отчество и контакты и перечитывает профиль", async () => {
    const user = userEvent.setup();
    let profile = me();
    let body: Schemas["ProfileUpdateRequest"] = {};
    let reads = 0;
    server.use(
      http.get("/api/v1/auth/me", () => {
        reads += 1;
        return HttpResponse.json(profile);
      }),
      http.get("/api/v1/departments", () => HttpResponse.json([])),
      http.patch("/api/v1/account", async ({ request }) => {
        body = (await request.json()) as Schemas["ProfileUpdateRequest"];
        profile = me({
          first_name: body.first_name ?? null,
          last_name: body.last_name ?? null,
          patronymic: body.patronymic ?? null,
          full_name: `${body.first_name ?? ""} ${body.last_name ?? ""}`,
          phone: "+79991234567",
          telegram: "anna_s",
        });
        return new HttpResponse(null, { status: 204 });
      }),
    );
    renderApp("/settings/profile");

    const first = await within(
      await screen.findByRole("region", { name: "Личные данные" }),
    ).findByLabelText("Имя");
    expect(first).toHaveValue("Анна");
    const email = screen.getByRole("region", { name: "Почта" });
    expect(within(email).getByText("anna@meridian-stroy.ru")).toBeInTheDocument();
    expect(
      within(email).getByRole("link", { name: "«Управление учётной записью»" }),
    ).toHaveAttribute("href", "/settings/account");
    const save = within(personal()).getByRole("button", { name: "Сохранить" });
    expect(save).toBeDisabled();

    const last = within(personal()).getByLabelText("Фамилия");
    await user.clear(last);
    await user.type(last, "  Петрова  ");
    await user.type(within(personal()).getByLabelText(/^Отчество/), "Сергеевна");
    await user.type(within(personal()).getByLabelText(/^Телефон/), "8 999 123 45 67");
    await user.type(within(personal()).getByLabelText(/^Telegram/), "@anna_s");
    const before = reads;
    await user.click(save);

    expect(await screen.findByText("Профиль сохранён")).toBeInTheDocument();
    expect(body).toEqual({
      first_name: "Анна",
      last_name: "Петрова",
      patronymic: "Сергеевна",
      phone: "8 999 123 45 67",
      telegram: "@anna_s",
    });
    expect(reads).toBeGreaterThan(before);
    // Новое имя — и в учётной записи в боковой панели; телефон — в одном виде.
    expect(
      await screen.findByRole("button", { name: /^Профиль: Анна Петрова/ }),
    ).toBeInTheDocument();
    expect(within(personal()).getByLabelText(/^Телефон/)).toHaveValue("+7 999 123-45-67");
  });

  it("не отправляет пустую фамилию; ошибку телефона показывает у поля", async () => {
    const user = userEvent.setup();
    let sent = 0;
    server.use(
      http.get("/api/v1/auth/me", () => HttpResponse.json(me())),
      http.get("/api/v1/departments", () => HttpResponse.json([])),
      http.patch("/api/v1/account", () => {
        sent += 1;
        return HttpResponse.json(
          { detail: "Укажите номер полностью, с кодом страны", code: "invalid_phone" },
          { status: 400 },
        );
      }),
    );
    renderApp("/settings/profile");

    const last = await within(
      await screen.findByRole("region", { name: "Личные данные" }),
    ).findByLabelText("Фамилия");
    await user.clear(last);
    await user.type(last, "   ");
    await user.click(within(personal()).getByRole("button", { name: "Сохранить" }));
    expect(await screen.findByText("Укажите фамилию.")).toBeInTheDocument();
    expect(sent).toBe(0);

    await user.type(last, "Смирнова");
    await user.type(within(personal()).getByLabelText(/^Телефон/), "12345");
    await user.click(within(personal()).getByRole("button", { name: "Сохранить" }));
    const phone = within(personal()).getByLabelText(/^Телефон/);
    await waitFor(() => expect(phone).toHaveAccessibleDescription(/с кодом страны/));
    expect(sent).toBe(1);
  });

  it("фото: загрузка файлом и удаление", async () => {
    const user = userEvent.setup();
    let profile = me();
    // Отправку FormData проверяет e2e в настоящем браузере: FormData из
    // jsdom fetch из Node не принимает. Здесь — что уходит и что дальше.
    const put = vi.spyOn(api, "PUT").mockImplementation(async () => {
      profile = me({ avatar_url: "/api/v1/avatars/a-1?v=1&exp=1&sig=x" });
      return {
        data: { avatar_url: profile.avatar_url },
        response: new Response(null, { status: 200 }),
      } as never;
    });
    server.use(
      http.get("/api/v1/auth/me", () => HttpResponse.json(profile)),
      http.get("/api/v1/departments", () => HttpResponse.json([])),
      http.delete("/api/v1/account/avatar", () => {
        profile = me();
        return new HttpResponse(null, { status: 204 });
      }),
    );
    const { container } = renderApp("/settings/profile");

    const photo = await screen.findByRole("region", { name: "Фото" });
    const input = container.querySelector<HTMLInputElement>('input[type="file"]');
    if (!input) throw new Error("no file input");
    const file = new File(["jpeg"], "me.jpg", { type: "image/jpeg" });
    await user.upload(input, file);
    expect(await screen.findByText("Фото обновлено")).toBeInTheDocument();
    expect(put).toHaveBeenCalledWith(
      "/api/v1/account/avatar",
      expect.objectContaining({ body: { file } }),
    );
    expect(await within(photo).findByRole("button", { name: "Удалить" })).toBeInTheDocument();

    await user.click(within(photo).getByRole("button", { name: "Удалить" }));
    expect(await screen.findByText("Фото удалено")).toBeInTheDocument();
    await waitFor(() =>
      expect(within(photo).queryByRole("button", { name: "Удалить" })).not.toBeInTheDocument(),
    );
    put.mockRestore();
  });

  it("фото больше 5 МБ не отправляется", async () => {
    const user = userEvent.setup();
    server.use(
      http.get("/api/v1/auth/me", () => HttpResponse.json(me())),
      http.get("/api/v1/departments", () => HttpResponse.json([])),
    );
    const { container } = renderApp("/settings/profile");
    await screen.findByRole("region", { name: "Фото" });
    const input = container.querySelector<HTMLInputElement>('input[type="file"]');
    if (!input) throw new Error("no file input");
    const big = new File([new Uint8Array(5 * 1024 * 1024 + 1)], "big.jpg", {
      type: "image/jpeg",
    });
    await user.upload(input, big);
    expect(await screen.findByText("Фото больше 5 МБ — выберите поменьше.")).toBeInTheDocument();
  });

  it("должность и отдел в текущей компании", async () => {
    const user = userEvent.setup();
    let body: unknown;
    server.use(
      http.get("/api/v1/auth/me", () => HttpResponse.json(me())),
      http.get("/api/v1/departments", () =>
        HttpResponse.json([{ id: "d-1", name: "Продажи", members: 3, unconfirmed: 0 }]),
      ),
      http.patch("/api/v1/people/:memberId", async ({ request, params }) => {
        body = { memberId: params.memberId, ...((await request.json()) as object) };
        return HttpResponse.json({});
      }),
    );
    renderApp("/settings/profile");

    const work = await screen.findByRole("region", { name: "Работа в «ООО «Меридиан Строй»»" });
    await user.type(within(work).getByLabelText(/^Должность/), " Менеджер ");
    await user.selectOptions(await within(work).findByLabelText(/^Отдел/), "d-1");
    await user.click(within(work).getByRole("button", { name: "Сохранить" }));

    expect(await screen.findByText("Сохранено")).toBeInTheDocument();
    expect(body).toEqual({ memberId: "m-1", position: "Менеджер", department_id: "d-1" });
  });
});
