import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { Schemas } from "../api/client";
import { getSession } from "../api/session";
import { createPasskey, PasskeyError, passkeysSupported } from "../lib/webauthn";
import { adminMe, me, tokens } from "../test/fixtures";
import { renderApp } from "../test/render";
import { server } from "../test/server";

// Браузерного WebAuthn в jsdom нет: окно создания ключа подменяем.
vi.mock("../lib/webauthn", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../lib/webauthn")>();
  return { ...actual, passkeysSupported: vi.fn(() => true), createPasskey: vi.fn() };
});

afterEach(() => {
  vi.mocked(passkeysSupported).mockReturnValue(true);
  vi.mocked(createPasskey).mockReset();
  vi.restoreAllMocks();
});

type Security = Schemas["SecurityResponse"];
type Session = Schemas["SessionResponse"];

const CODES = [
  "K7QM-4XPA",
  "R2DT-9WEC",
  "M4HZ-7KQP",
  "B8NV-3TXS",
  "Q6JF-2YRD",
  "W5CK-8PLM",
  "T3XG-6HNA",
  "D9RS-4VBE",
  "H2PW-5MZK",
  "N7EY-1QCT",
];

function security(overrides: Partial<Security> = {}): Security {
  return {
    totp_enabled: false,
    passkeys: [],
    backup_codes_left: 0,
    strong_required: false,
    ...overrides,
  };
}

function session(overrides: Partial<Session> = {}): Session {
  return {
    id: "s-1",
    device: "Chrome, macOS",
    ip: "192.0.2.10",
    started_at: "2026-10-01T08:00:00Z",
    last_active_at: "2026-10-03T08:00:00Z",
    current: true,
    ...overrides,
  };
}

/** Вошёл человек; состояние защиты и сеансы — из переданных значений. */
function signedIn({
  profile = me(),
  state = security(),
  sessions = [session()],
}: {
  profile?: Schemas["MeResponse"];
  state?: Security;
  sessions?: Session[];
} = {}) {
  server.use(
    http.get("/api/v1/auth/me", () => HttpResponse.json(profile)),
    http.get("/api/v1/account/security", () => HttpResponse.json(state)),
    http.get("/api/v1/auth/sessions", () => HttpResponse.json(sessions)),
  );
}

describe("безопасность: обязательная защита", () => {
  it("администратору без приложения и ключа — заметное предупреждение", async () => {
    signedIn({
      profile: adminMe({ mfa: { strong: false, strong_required: true } }),
      state: security({ strong_required: true }),
    });
    renderApp("/settings/security");

    expect(
      await screen.findByText("Включите приложение-аутентификатор или ключ доступа"),
    ).toBeInTheDocument();
    expect(screen.getByText(/данные компании недоступны/)).toBeInTheDocument();
    expect(document.title).toBe("Безопасность — kronto");
  });

  it("единственный надёжный способ при обязательной защите отключить нельзя", async () => {
    signedIn({
      profile: adminMe(),
      state: security({ totp_enabled: true, strong_required: true, backup_codes_left: 10 }),
    });
    server.use(http.get("/api/v1/usage", () => HttpResponse.json({ warning: false })));
    renderApp("/settings/security");

    expect(await screen.findByRole("button", { name: "Отключить приложение" })).toBeDisabled();
    expect(screen.getByText(/сначала добавьте ключ доступа/)).toBeInTheDocument();
  });
});

describe("безопасность: пароль", () => {
  it("меняет пароль, очищает поля и перечитывает сеансы", async () => {
    const user = userEvent.setup();
    let body: unknown;
    let sessionReads = 0;
    signedIn();
    server.use(
      http.get("/api/v1/auth/sessions", () => {
        sessionReads += 1;
        return HttpResponse.json([session()]);
      }),
      http.post("/api/v1/auth/change-password", async ({ request }) => {
        body = await request.json();
        return HttpResponse.json(tokens(2));
      }),
    );
    renderApp("/settings/security");

    await user.type(await screen.findByLabelText("Текущий пароль"), "старый пароль входа");
    await user.type(screen.getByLabelText("Новый пароль"), "длинная фраза для входа");
    await user.type(screen.getByLabelText("Повторите новый пароль"), "длинная фраза для входа");
    await waitFor(() => expect(sessionReads).toBe(1));
    await user.click(screen.getByRole("button", { name: "Сменить пароль" }));

    expect(await screen.findByText(/Пароль изменён/)).toBeInTheDocument();
    expect(body).toEqual({
      current_password: "старый пароль входа",
      new_password: "длинная фраза для входа",
    });
    expect(getSession()?.accessToken).toBe("access-2");
    expect(screen.getByLabelText("Текущий пароль")).toHaveValue("");
    expect(screen.getByLabelText("Новый пароль")).toHaveValue("");
    await waitFor(() => expect(sessionReads).toBe(2));
  });

  it("не отправляет несовпадающие пароли", async () => {
    const user = userEvent.setup();
    signedIn();
    renderApp("/settings/security");

    await user.type(await screen.findByLabelText("Текущий пароль"), "старый пароль входа");
    await user.type(screen.getByLabelText("Новый пароль"), "длинная фраза для входа");
    await user.type(screen.getByLabelText("Повторите новый пароль"), "другая фраза для входа");
    await user.click(screen.getByRole("button", { name: "Сменить пароль" }));

    expect(await screen.findByText("Пароли не совпадают.")).toBeInTheDocument();
  });
});

describe("безопасность: приложение-аутентификатор", () => {
  it("QR и ключ → код → резервные коды показываются один раз", async () => {
    const user = userEvent.setup();
    let state = security();
    let profile = me();
    let enableBody: unknown;
    signedIn();
    server.use(
      http.get("/api/v1/auth/me", () => HttpResponse.json(profile)),
      http.get("/api/v1/account/security", () => HttpResponse.json(state)),
      http.post("/api/v1/account/totp/setup", () =>
        HttpResponse.json({
          secret: "JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP",
          otpauth_uri:
            "otpauth://totp/kronto%3Aanna%40meridian-stroy.ru?secret=JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP&issuer=kronto&digits=6&period=30",
          setup_token: "setup-token-0123456789abcdef",
        }),
      ),
      http.post("/api/v1/account/totp/enable", async ({ request }) => {
        enableBody = await request.json();
        state = security({ totp_enabled: true, backup_codes_left: 10 });
        profile = me({ mfa: { strong: true, strong_required: false } });
        return HttpResponse.json({ backup_codes: CODES });
      }),
    );
    renderApp("/settings/security");

    const section = await screen.findByRole("region", { name: "Приложение-аутентификатор" });
    expect(within(section).getByText("выключено")).toBeInTheDocument();
    // Без надёжного фактора резервных кодов нет.
    expect(screen.queryByRole("region", { name: "Резервные коды" })).not.toBeInTheDocument();
    await user.click(within(section).getByRole("button", { name: "Подключить приложение" }));

    const setup = await screen.findByRole("dialog", { name: "Подключение приложения" });
    const qr = within(setup).getByRole("img", { name: "QR-код для приложения-аутентификатора" });
    expect(qr.querySelector("path")?.getAttribute("d")).toMatch(/^M\d+ \d+h1v1h-1z/);
    expect(within(setup).getByText("JBSW Y3DP EHPK 3PXP JBSW Y3DP EHPK 3PXP")).toBeInTheDocument();
    const enable = within(setup).getByRole("button", { name: "Включить" });
    expect(enable).toBeDisabled();
    await user.type(within(setup).getByLabelText("Код из приложения"), "12a3 456");
    expect(within(setup).getByLabelText("Код из приложения")).toHaveValue("123456");
    await user.click(enable);

    const codes = await screen.findByRole("dialog", { name: "Резервные коды" });
    expect(enableBody).toEqual({ setup_token: "setup-token-0123456789abcdef", code: "123456" });
    expect(
      within(codes)
        .getAllByRole("listitem")
        .map((item) => item.textContent),
    ).toEqual(CODES);
    expect(
      screen.queryByRole("dialog", { name: "Подключение приложения" }),
    ).not.toBeInTheDocument();

    await user.click(within(codes).getByRole("button", { name: "Скопировать все" }));
    expect(await navigator.clipboard.readText()).toBe(CODES.join("\n"));

    // Закрыть, не отметив, что коды сохранены, нельзя.
    await user.click(within(codes).getByRole("button", { name: "Готово" }));
    expect(within(codes).getByRole("alert")).toHaveTextContent(/Сначала сохраните коды/);
    await user.keyboard("{Escape}");
    expect(screen.getByRole("dialog", { name: "Резервные коды" })).toBeInTheDocument();

    await user.click(within(codes).getByRole("checkbox", { name: "Я сохранил коды" }));
    await user.click(within(codes).getByRole("button", { name: "Готово" }));
    await waitFor(() =>
      expect(screen.queryByRole("dialog", { name: "Резервные коды" })).not.toBeInTheDocument(),
    );
    expect(screen.queryByText("K7QM-4XPA")).not.toBeInTheDocument();

    expect(await within(section).findByText("включено")).toBeInTheDocument();
    const backup = await screen.findByRole("region", { name: "Резервные коды" });
    expect(within(backup).getByText("осталось 10 из 10")).toBeInTheDocument();
  });

  it("неверный код — сообщение сервера, поле очищается", async () => {
    const user = userEvent.setup();
    signedIn();
    server.use(
      http.post("/api/v1/account/totp/setup", () =>
        HttpResponse.json({
          secret: "JBSWY3DPEHPK3PXP",
          otpauth_uri: "otpauth://totp/kronto?secret=JBSWY3DPEHPK3PXP",
          setup_token: "setup-token-0123456789abcdef",
        }),
      ),
      http.post("/api/v1/account/totp/enable", () =>
        HttpResponse.json(
          {
            detail: "Код не подошёл или устарел — попробуйте ещё раз",
            code: "invalid_second_factor",
          },
          { status: 400 },
        ),
      ),
    );
    renderApp("/settings/security");

    await user.click(await screen.findByRole("button", { name: "Подключить приложение" }));
    const setup = await screen.findByRole("dialog", { name: "Подключение приложения" });
    await user.type(within(setup).getByLabelText("Код из приложения"), "000000");
    await user.click(within(setup).getByRole("button", { name: "Включить" }));

    expect(
      await within(setup).findByText("Код не подошёл или устарел — попробуйте ещё раз"),
    ).toBeInTheDocument();
    expect(within(setup).getByLabelText("Код из приложения")).toHaveValue("");
  });

  it("время настройки вышло — новый секрет и новый QR-код", async () => {
    const user = userEvent.setup();
    const secrets = ["JBSWY3DPEHPK3PXP", "KRSXG5CTMVRXEZLU"];
    let setups = 0;
    signedIn();
    server.use(
      http.post("/api/v1/account/totp/setup", () => {
        const secret = secrets[setups] ?? "";
        setups += 1;
        return HttpResponse.json({
          secret,
          otpauth_uri: `otpauth://totp/kronto?secret=${secret}`,
          setup_token: `setup-token-${setups}-0123456789abcdef`,
        });
      }),
      http.post("/api/v1/account/totp/enable", () =>
        HttpResponse.json(
          { detail: "Время настройки вышло — начните заново", code: "setup_expired" },
          { status: 400 },
        ),
      ),
    );
    renderApp("/settings/security");

    await user.click(await screen.findByRole("button", { name: "Подключить приложение" }));
    let setup = await screen.findByRole("dialog", { name: "Подключение приложения" });
    await user.type(within(setup).getByLabelText("Код из приложения"), "000000");
    await user.click(within(setup).getByRole("button", { name: "Включить" }));

    // Тост и его объявление для скринридера.
    expect(await screen.findAllByText(/Время настройки вышло/)).not.toHaveLength(0);
    await waitFor(() => expect(setups).toBe(2));
    setup = await screen.findByRole("dialog", { name: "Подключение приложения" });
    expect(within(setup).getByText(/KRSX/)).toBeInTheDocument();
  });

  it("отключает приложение по паролю и коду", async () => {
    const user = userEvent.setup();
    let state = security({ totp_enabled: true, backup_codes_left: 8 });
    let body: unknown;
    let attempts = 0;
    signedIn({ profile: me({ mfa: { strong: true, strong_required: false } }) });
    server.use(
      http.get("/api/v1/account/security", () => HttpResponse.json(state)),
      http.post("/api/v1/account/totp/disable", async ({ request }) => {
        attempts += 1;
        body = await request.json();
        if (attempts === 1) {
          return HttpResponse.json(
            { detail: "Пароль указан неверно", code: "invalid_password" },
            { status: 400 },
          );
        }
        state = security();
        return new HttpResponse(null, { status: 204 });
      }),
    );
    renderApp("/settings/security");

    await user.click(await screen.findByRole("button", { name: "Отключить приложение" }));
    const dialog = await screen.findByRole("dialog", { name: "Отключить приложение?" });
    await user.type(within(dialog).getByLabelText("Пароль от учётной записи"), "не тот");
    await user.type(
      within(dialog).getByLabelText("Код из приложения или резервный код"),
      "k7qm-4xpa",
    );
    await user.click(within(dialog).getByRole("button", { name: "Отключить" }));
    expect(await within(dialog).findByText("Пароль указан неверно")).toBeInTheDocument();
    expect(body).toEqual({ password: "не тот", code: "K7QM-4XPA" });

    await user.type(within(dialog).getByLabelText("Код из приложения или резервный код"), "654321");
    await user.click(within(dialog).getByRole("button", { name: "Отключить" }));

    expect(await screen.findByText("Приложение отключено")).toBeInTheDocument();
    expect(body).toEqual({ password: "не тот", code: "654321" });
    expect(
      await screen.findByRole("button", { name: "Подключить приложение" }),
    ).toBeInTheDocument();
  });
});

describe("безопасность: ключи доступа", () => {
  it("добавляет ключ: параметры сервера → браузер → регистрация, затем резервные коды", async () => {
    const user = userEvent.setup();
    let state = security();
    let body: unknown;
    const options = { challenge: "Y2hhbGxlbmdl", rp: { name: "kronto" } };
    vi.mocked(createPasskey).mockResolvedValue({ id: "cred-1", type: "public-key" });
    signedIn();
    server.use(
      http.get("/api/v1/account/security", () => HttpResponse.json(state)),
      http.post("/api/v1/account/passkeys/options", () =>
        HttpResponse.json({ options, setup_token: "passkey-setup-0123456789" }),
      ),
      http.post("/api/v1/account/passkeys", async ({ request }) => {
        body = await request.json();
        const passkey = {
          id: "pk-1",
          name: "Рабочий ноутбук",
          created_at: "2026-10-03T09:00:00Z",
          last_used_at: null,
        };
        state = security({ passkeys: [passkey], backup_codes_left: 10 });
        return HttpResponse.json({ passkey, backup_codes: CODES }, { status: 201 });
      }),
    );
    renderApp("/settings/security");

    const section = await screen.findByRole("region", { name: "Ключи доступа" });
    expect(within(section).getByText("Ключей пока нет.")).toBeInTheDocument();
    await user.click(within(section).getByRole("button", { name: "Добавить ключ" }));
    const dialog = await screen.findByRole("dialog", { name: "Новый ключ доступа" });
    await user.type(within(dialog).getByLabelText(/Название/), "  Рабочий   ноутбук ");
    await user.click(within(dialog).getByRole("button", { name: "Создать ключ" }));

    expect(await screen.findByRole("dialog", { name: "Резервные коды" })).toBeInTheDocument();
    expect(createPasskey).toHaveBeenCalledWith(options);
    expect(body).toEqual({
      setup_token: "passkey-setup-0123456789",
      credential: { id: "cred-1", type: "public-key" },
      name: "Рабочий ноутбук",
    });
    expect(screen.getByText("Ключ «Рабочий ноутбук» добавлен")).toBeInTheDocument();
    expect(await within(section).findByText("Рабочий ноутбук")).toBeInTheDocument();
    expect(within(section).getByText("Для входа ещё не использовался")).toBeInTheDocument();
  });

  it("окно браузера закрыли — понятная ошибка, ключ не регистрируется", async () => {
    const user = userEvent.setup();
    let registered = false;
    vi.mocked(createPasskey).mockRejectedValue(
      new PasskeyError("Ключ не подтверждён: окно закрыто или вышло время. Попробуйте ещё раз."),
    );
    signedIn();
    server.use(
      http.post("/api/v1/account/passkeys/options", () =>
        HttpResponse.json({
          options: { challenge: "eA" },
          setup_token: "passkey-setup-0123456789",
        }),
      ),
      http.post("/api/v1/account/passkeys", () => {
        registered = true;
        return HttpResponse.json({}, { status: 500 });
      }),
    );
    renderApp("/settings/security");

    await user.click(await screen.findByRole("button", { name: "Добавить ключ" }));
    const dialog = await screen.findByRole("dialog", { name: "Новый ключ доступа" });
    await user.click(within(dialog).getByRole("button", { name: "Создать ключ" }));

    expect(await within(dialog).findByText(/Ключ не подтверждён/)).toBeInTheDocument();
    expect(registered).toBe(false);
  });

  it("без поддержки в браузере кнопки добавления нет — есть объяснение", async () => {
    vi.mocked(passkeysSupported).mockReturnValue(false);
    signedIn();
    renderApp("/settings/security");

    const section = await screen.findByRole("region", { name: "Ключи доступа" });
    expect(within(section).getByText(/браузер не поддерживает ключи доступа/)).toBeInTheDocument();
    expect(
      within(section).queryByRole("button", { name: "Добавить ключ" }),
    ).not.toBeInTheDocument();
  });

  it("удаляет ключ после пароля", async () => {
    const user = userEvent.setup();
    const passkey = {
      id: "pk-1",
      name: "Телефон",
      created_at: "2026-09-20T09:00:00Z",
      last_used_at: "2026-10-02T09:00:00Z",
    };
    let state = security({ totp_enabled: true, passkeys: [passkey], backup_codes_left: 9 });
    let body: unknown;
    signedIn({ profile: me({ mfa: { strong: true, strong_required: false } }) });
    server.use(
      http.get("/api/v1/account/security", () => HttpResponse.json(state)),
      http.post("/api/v1/account/passkeys/pk-1/delete", async ({ request }) => {
        body = await request.json();
        state = security({ totp_enabled: true, backup_codes_left: 9 });
        return new HttpResponse(null, { status: 204 });
      }),
    );
    renderApp("/settings/security");

    await user.click(await screen.findByRole("button", { name: "Удалить ключ «Телефон»" }));
    const dialog = await screen.findByRole("dialog", { name: "Удалить ключ «Телефон»?" });
    await user.type(within(dialog).getByLabelText("Пароль от учётной записи"), "мой пароль");
    await user.click(within(dialog).getByRole("button", { name: "Удалить" }));

    expect(await screen.findByText("Ключ удалён")).toBeInTheDocument();
    expect(body).toEqual({ password: "мой пароль" });
    const section = screen.getByRole("region", { name: "Ключи доступа" });
    expect(await within(section).findByText("Ключей пока нет.")).toBeInTheDocument();
  });
});

describe("безопасность: резервные коды", () => {
  it("кодов мало — предупреждение; новые коды по паролю и скачиваются файлом", async () => {
    const user = userEvent.setup();
    let body: unknown;
    let file: Blob | null = null;
    vi.spyOn(URL, "createObjectURL").mockImplementation((blob) => {
      file = blob as Blob;
      return "blob:codes";
    });
    vi.spyOn(URL, "revokeObjectURL").mockImplementation(() => undefined);
    const click = vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => {});
    signedIn({
      profile: me({ mfa: { strong: true, strong_required: false } }),
      state: security({ totp_enabled: true, backup_codes_left: 2 }),
    });
    server.use(
      http.post("/api/v1/account/backup-codes", async ({ request }) => {
        body = await request.json();
        return HttpResponse.json({ backup_codes: CODES });
      }),
    );
    renderApp("/settings/security");

    const section = await screen.findByRole("region", { name: "Резервные коды" });
    expect(within(section).getByText("осталось 2 из 10")).toBeInTheDocument();
    expect(within(section).getByText(/Кодов почти не осталось/)).toBeInTheDocument();
    await user.click(within(section).getByRole("button", { name: "Выпустить новые коды" }));
    const confirm = await screen.findByRole("dialog", { name: "Выпустить новые коды?" });
    await user.type(within(confirm).getByLabelText("Пароль от учётной записи"), "мой пароль");
    await user.click(within(confirm).getByRole("button", { name: "Выпустить" }));

    const codes = await screen.findByRole("dialog", { name: "Резервные коды" });
    expect(body).toEqual({ password: "мой пароль" });
    await user.click(within(codes).getByRole("button", { name: "Скачать .txt" }));
    expect(click).toHaveBeenCalledOnce();
    expect(file).not.toBeNull();
    const text = await (file as unknown as Blob).text();
    expect(text).toContain("anna@meridian-stroy.ru");
    for (const code of CODES) expect(text).toContain(code);
  });
});

describe("безопасность: сеансы", () => {
  it("это устройство — первым и без «Завершить»; другой сеанс завершается", async () => {
    const user = userEvent.setup();
    let list = [
      session({
        id: "s-2",
        device: "Firefox, Windows",
        ip: "198.51.100.7",
        current: false,
        last_active_at: "2026-10-02T08:00:00Z",
      }),
      session(),
    ];
    let ended: string | undefined;
    signedIn();
    server.use(
      http.get("/api/v1/auth/sessions", () => HttpResponse.json(list)),
      http.post("/api/v1/auth/sessions/:id/end", ({ params }) => {
        ended = params.id as string;
        list = list.filter((item) => item.id !== ended);
        return new HttpResponse(null, { status: 204 });
      }),
    );
    renderApp("/settings/security");

    const table = await screen.findByRole("table", { name: "Сеансы" });
    const rows = await within(table).findAllByRole("row");
    expect(rows).toHaveLength(3);
    expect(within(rows[1]!).getByText("Chrome, macOS")).toBeInTheDocument();
    expect(within(rows[1]!).getByText("Это устройство")).toBeInTheDocument();
    expect(within(rows[1]!).queryByRole("button")).not.toBeInTheDocument();
    expect(within(rows[2]!).getByText("198.51.100.7")).toBeInTheDocument();

    await user.click(
      within(rows[2]!).getByRole("button", { name: "Завершить сеанс: Firefox, Windows" }),
    );

    expect(await screen.findByText(/Сеанс завершён/)).toBeInTheDocument();
    expect(ended).toBe("s-2");
    await waitFor(() => expect(within(table).getAllByRole("row")).toHaveLength(2));
  });

  it("«Выйти на всех устройствах» — после подтверждения выход и здесь", async () => {
    const user = userEvent.setup();
    let everywhere = false;
    signedIn();
    server.use(
      http.post("/api/v1/auth/logout-all", () => {
        everywhere = true;
        return new HttpResponse(null, { status: 204 });
      }),
      http.post("/api/v1/auth/logout", () => new HttpResponse(null, { status: 204 })),
    );
    const { router } = renderApp("/settings/security");

    await user.click(await screen.findByRole("button", { name: "Выйти на всех устройствах" }));
    const dialog = await screen.findByRole("dialog", { name: "Выйти на всех устройствах?" });
    expect(everywhere).toBe(false);
    await user.click(within(dialog).getByRole("button", { name: "Выйти везде" }));

    await waitFor(() => expect(router.state.location.pathname).toBe("/login"));
    expect(everywhere).toBe(true);
    expect(getSession()).toBeNull();
  });
});
