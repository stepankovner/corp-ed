import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { me } from "../test/fixtures";
import { renderApp } from "../test/render";
import { server } from "../test/server";

const BITRIX = {
  id: "c-2",
  kind: "bitrix24",
  name: "Битрикс24",
  oauth: true,
  grant_status: null,
  grant_error_code: null,
  credential_fields: [],
};

/** Вид без OAuth: сотрудник вводит логин и пароль приложения сам. */
const NEXTCLOUD = {
  id: "c-5",
  kind: "nextcloud",
  name: "Nextcloud",
  oauth: false,
  grant_status: null as string | null,
  grant_error_code: null,
  credential_fields: [
    { name: "login", title: "Логин", required: true, secret: false },
    { name: "password", title: "Пароль приложения", required: true, secret: true },
  ],
};

function signedIn() {
  server.use(http.get("/api/v1/auth/me", () => HttpResponse.json(me())));
}

beforeEach(() => localStorage.clear());

describe("где ищет ассистент", () => {
  it("показывает папки сотрудника и источники компании с его состоянием", async () => {
    signedIn();
    server.use(
      http.get("/api/v1/connectors/mine", () => HttpResponse.json([BITRIX])),
      http.get("/api/v1/sources/mine", () =>
        HttpResponse.json({
          files: [
            { folder_id: null, name: "Общие документы", restricted: false, documents: 12 },
            { folder_id: "f-1", name: "Бухгалтерия", restricted: true, documents: 3 },
          ],
          connectors: [
            {
              id: "c-1",
              kind: "confluence",
              name: "Confluence",
              mode: "organization",
              working: true,
              grant_status: null,
            },
            {
              id: "c-2",
              kind: "bitrix24",
              name: "Битрикс24",
              mode: "per_user",
              working: true,
              grant_status: null,
            },
          ],
        }),
      ),
    );
    renderApp("/settings/connections");

    const section = await screen.findByRole("region", { name: "Где ищет ассистент" });
    expect(await within(section).findByText("Общие документы")).toBeInTheDocument();
    expect(within(section).getByText("12 документов")).toBeInTheDocument();
    expect(within(section).getByText("Бухгалтерия")).toBeInTheDocument();
    expect(within(section).getByText("для вашего отдела")).toBeInTheDocument();
    expect(within(section).getByText("3 документа")).toBeInTheDocument();
    expect(within(section).getByText("подключено компанией")).toBeInTheDocument();
    expect(within(section).getByText("ищет")).toBeInTheDocument();
    expect(within(section).getByText("ваш личный аккаунт")).toBeInTheDocument();
    expect(within(section).getByText("не подключён")).toBeInTheDocument();

    expect(screen.getByRole("heading", { name: "Ваши аккаунты" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Подключить" })).toBeInTheDocument();
  });

  it("пусто — объясняет, что ассистенту пока не из чего отвечать", async () => {
    signedIn();
    renderApp("/settings/connections");
    const section = await screen.findByRole("region", { name: "Где ищет ассистент" });
    expect(
      await within(section).findByText(/Пока ассистенту не из чего отвечать/),
    ).toBeInTheDocument();
  });
});

describe("подключение своего аккаунта", () => {
  it("адрес входа не http(s) — не переходим, объясняем", async () => {
    signedIn();
    let started = false;
    server.use(
      http.get("/api/v1/connectors/mine", () => HttpResponse.json([BITRIX])),
      http.post("/api/v1/connectors/c-2/oauth/start", () => {
        started = true;
        return HttpResponse.json({ authorize_url: "javascript:alert(document.domain)" });
      }),
    );
    const user = userEvent.setup();
    renderApp("/settings/connections");

    await user.click(await screen.findByRole("button", { name: "Подключить" }));

    expect(
      await screen.findByText("Источник вернул некорректный адрес входа. Попробуйте позже."),
    ).toBeInTheDocument();
    // Ошибка — из проверки адреса: переход (onSuccess) не вызывался.
    expect(started).toBe(true);
  });
});

describe("подключение вводом учётных данных", () => {
  /** Сервер с одним источником без OAuth: PUT /mine — грант, DELETE — нет гранта. */
  function credentialServer(
    initial: string | null = null,
    put: (body: unknown) => Response | undefined = () => undefined,
  ) {
    let status = initial;
    const sent: unknown[] = [];
    let deleted = 0;
    server.use(
      http.get("/api/v1/connectors/mine", () =>
        HttpResponse.json([{ ...NEXTCLOUD, grant_status: status }]),
      ),
      http.put("/api/v1/connectors/c-5/mine", async ({ request }) => {
        const body = await request.json();
        sent.push(body);
        const failed = put(body);
        if (failed) return failed;
        status = "active";
        return new HttpResponse(null, { status: 204 });
      }),
      http.delete("/api/v1/connectors/c-5/mine", () => {
        deleted += 1;
        status = null;
        return new HttpResponse(null, { status: 204 });
      }),
    );
    return { sent, deleted: () => deleted };
  }

  it("форма с полями источника: пароль скрыт и не запоминается браузером", async () => {
    signedIn();
    credentialServer();
    renderApp("/settings/connections");

    const login = await screen.findByLabelText("Логин");
    const password = screen.getByLabelText("Пароль приложения");
    expect(login).toHaveAttribute("type", "text");
    expect(login).toHaveAttribute("autocomplete", "off");
    expect(password).toHaveAttribute("type", "password");
    expect(password).toHaveAttribute("autocomplete", "new-password");
    expect(screen.queryByText("Подключается через администратора.")).not.toBeInTheDocument();
  });

  it("ввод и отправка — подключён, можно изменить данные и отключить", async () => {
    signedIn();
    const api = credentialServer();
    const user = userEvent.setup();
    renderApp("/settings/connections");

    await user.type(await screen.findByLabelText("Логин"), "ivan");
    await user.type(screen.getByLabelText("Пароль приложения"), "app-pass");
    await user.click(screen.getByRole("button", { name: "Подключить" }));

    expect(await screen.findByText("подключён")).toBeInTheDocument();
    expect(api.sent).toEqual([{ credentials: { login: "ivan", password: "app-pass" } }]);
    expect(screen.getByText("Аккаунт подключён")).toBeInTheDocument();
    expect(screen.queryByLabelText("Пароль приложения")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Изменить данные" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Отключить" })).toBeInTheDocument();
  });

  it("ошибка сервера — текст по коду, а не служебное сообщение", async () => {
    signedIn();
    credentialServer(null, () =>
      HttpResponse.json(
        { detail: "Источник не принял учётные данные: auth_failed", code: "auth_failed" },
        { status: 422 },
      ),
    );
    const user = userEvent.setup();
    renderApp("/settings/connections");

    await user.type(await screen.findByLabelText("Логин"), "ivan");
    await user.type(screen.getByLabelText("Пароль приложения"), "wrong");
    await user.click(screen.getByRole("button", { name: "Подключить" }));

    expect(
      await screen.findByText("Источник отклонил доступ — проверьте учётные данные"),
    ).toBeInTheDocument();
    expect(screen.queryByText(/Источник не принял учётные данные/)).not.toBeInTheDocument();
    expect(screen.getByText("не подключён")).toBeInTheDocument();
    // Пароль стёрт: вводить заново, логин остаётся.
    expect(screen.getByLabelText("Пароль приложения")).toHaveValue("");
    expect(screen.getByLabelText("Логин")).toHaveValue("ivan");
  });

  it("источник не ответил вовремя — объясняем про источник, а не про разбор файла", async () => {
    signedIn();
    credentialServer(null, () =>
      HttpResponse.json({ detail: "timeout", code: "timeout" }, { status: 422 }),
    );
    const user = userEvent.setup();
    renderApp("/settings/connections");

    await user.type(await screen.findByLabelText("Логин"), "ivan");
    await user.type(screen.getByLabelText("Пароль приложения"), "x");
    await user.click(screen.getByRole("button", { name: "Подключить" }));

    expect(
      await screen.findByText("Источник не ответил вовремя — попробуйте позже"),
    ).toBeInTheDocument();
  });

  it("изменение данных: форма по кнопке, отмена её прячет, сохранение отправляет", async () => {
    signedIn();
    const api = credentialServer("active");
    const user = userEvent.setup();
    renderApp("/settings/connections");

    await user.click(await screen.findByRole("button", { name: "Изменить данные" }));
    await user.click(screen.getByRole("button", { name: "Отмена" }));
    expect(screen.queryByLabelText("Логин")).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Изменить данные" }));
    await user.type(screen.getByLabelText("Логин"), "ivan");
    await user.type(screen.getByLabelText("Пароль приложения"), "new-pass");
    await user.click(screen.getByRole("button", { name: "Сохранить" }));

    await vi.waitFor(() =>
      expect(api.sent).toEqual([{ credentials: { login: "ivan", password: "new-pass" } }]),
    );
    expect(await screen.findByText("Аккаунт подключён")).toBeInTheDocument();
    expect(screen.queryByLabelText("Логин")).not.toBeInTheDocument();
  });

  it("отключение — через подтверждение, после него снова форма", async () => {
    signedIn();
    const api = credentialServer("active");
    const user = userEvent.setup();
    renderApp("/settings/connections");

    await user.click(await screen.findByRole("button", { name: "Отключить" }));
    const dialog = await screen.findByRole("dialog");
    await user.click(within(dialog).getByRole("button", { name: "Отключить" }));

    await vi.waitFor(() => expect(api.deleted()).toBe(1));
    expect(await screen.findByLabelText("Логин")).toBeInTheDocument();
    expect(screen.getByText("не подключён")).toBeInTheDocument();
  });

  it("вид без полей и без OAuth — подключает администратор", async () => {
    signedIn();
    server.use(
      http.get("/api/v1/connectors/mine", () =>
        HttpResponse.json([{ ...NEXTCLOUD, credential_fields: [] }]),
      ),
    );
    renderApp("/settings/connections");
    expect(await screen.findByText("Подключается через администратора.")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Подключить" })).not.toBeInTheDocument();
  });
});

describe("предложение подключить свой аккаунт", () => {
  it("на пустом экране чата ведёт в настройки и скрывается насовсем", async () => {
    signedIn();
    const yandex = { ...BITRIX, id: "c-3", kind: "yandex360", name: "Яндекс 360" };
    server.use(http.get("/api/v1/connectors/mine", () => HttpResponse.json([BITRIX, yandex])));
    const user = userEvent.setup();
    const { unmount } = renderApp("/");

    const note = await screen.findByRole("note");
    expect(note).toHaveTextContent(
      "Подключите свой Битрикс24, чтобы ассистент искал и по вашим файлам.",
    );
    expect(within(note).getByRole("link", { name: "Подключить" })).toHaveAttribute(
      "href",
      "/settings/connections",
    );
    await user.click(within(note).getByRole("button", { name: "Скрыть" }));
    expect(await screen.findByRole("note")).toHaveTextContent("Подключите свой Яндекс 360");

    // Скрытое помнится после перезагрузки: предлагаем следующий источник.
    unmount();
    renderApp("/");
    expect(await screen.findByRole("note")).toHaveTextContent("Подключите свой Яндекс 360");
  });

  it("предлагает и источник, где вводят логин и пароль", async () => {
    signedIn();
    server.use(http.get("/api/v1/connectors/mine", () => HttpResponse.json([NEXTCLOUD])));
    renderApp("/");
    expect(await screen.findByRole("note")).toHaveTextContent("Подключите свой Nextcloud");
  });

  it("не показывает, когда аккаунт подключён", async () => {
    signedIn();
    let asked = false;
    server.use(
      http.get("/api/v1/connectors/mine", () => {
        asked = true;
        return HttpResponse.json([{ ...BITRIX, grant_status: "active" }]);
      }),
    );
    renderApp("/");
    await screen.findByRole("heading", { name: /Здравствуйте/ });
    await vi.waitFor(() => expect(asked).toBe(true));
    expect(screen.queryByRole("note")).not.toBeInTheDocument();
  });
});
