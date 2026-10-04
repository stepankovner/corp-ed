import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { describe, expect, it } from "vitest";

import type { Schemas } from "../api/client";
import { adminMe } from "../test/fixtures";
import { renderApp } from "../test/render";
import { server } from "../test/server";

type Person = Schemas["StaffPersonResponse"];

function person(overrides: Partial<Person> = {}): Person {
  return {
    id: "a-1",
    email: "petr@meridian-stroy.ru",
    full_name: "Пётр Орлов",
    email_verified: true,
    created_at: "2026-09-01T10:00:00Z",
    last_login_at: null,
    must_change_password: false,
    totp: true,
    passkeys: 2,
    backup_codes: 8,
    sessions: 3,
    staff: false,
    companies: [
      {
        tenant_id: "t-1",
        company_name: "ООО «Меридиан Строй»",
        role: "admin",
        status: "active",
        last_login_at: null,
      },
      {
        tenant_id: "t-2",
        company_name: "АО «Север»",
        role: "employee",
        status: "blocked",
        last_login_at: null,
      },
    ],
    ...overrides,
  };
}

function shell(found: Person[]) {
  const searched: string[] = [];
  const resets: string[] = [];
  server.use(
    http.get("/api/v1/auth/me", () => HttpResponse.json(adminMe({ staff: true }))),
    http.get("/api/v1/usage", () => HttpResponse.json({ warning: false })),
    http.get("/api/v1/staff/overview", () =>
      HttpResponse.json({
        companies: 1,
        active_companies: 1,
        pilots_ending: 0,
        requests_new: 0,
        accounts: 3,
      }),
    ),
    http.get("/api/v1/staff/people", ({ request }) => {
      const q = new URL(request.url).searchParams.get("q") ?? "";
      searched.push(q);
      return HttpResponse.json(q.length >= 3 ? found : []);
    }),
    http.post("/api/v1/staff/people/:accountId/password-reset", ({ params }) => {
      resets.push(String(params.accountId));
      return new HttpResponse(null, { status: 202 });
    }),
  );
  return { searched, resets };
}

describe("люди: помощь со входом", () => {
  it("ищет от трёх символов и показывает, как человек входит и где состоит", async () => {
    const user = userEvent.setup();
    const { searched } = shell([
      person(),
      person({
        id: "a-2",
        email: "olga@sever.ru",
        full_name: null,
        email_verified: false,
        must_change_password: true,
        totp: false,
        passkeys: 0,
        backup_codes: 0,
        sessions: 0,
        staff: true,
        companies: [],
      }),
    ]);
    renderApp("/staff/people");

    const input = await screen.findByRole("searchbox", { name: "Почта или имя" });
    expect(document.title).toBe("Люди — kronto");

    await user.type(input, "пё");
    await user.click(screen.getByRole("button", { name: "Найти" }));
    expect(screen.getByRole("alert")).toHaveTextContent("Нужно хотя бы 3 символа.");
    expect(input).toHaveAttribute("aria-invalid", "true");
    expect(searched).toEqual([]);

    // Поиск и по Enter: форма отправляется из поля.
    await user.type(input, "тр{Enter}");
    const petr = await screen.findByRole("listitem", { name: "Пётр Орлов" });
    expect(searched).toEqual(["пётр"]);
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();

    expect(within(petr).getByText("petr@meridian-stroy.ru")).toBeInTheDocument();
    expect(within(petr).getByText("не входил")).toBeInTheDocument();
    expect(
      within(petr).getByText("приложение · ключи доступа: 2 · резервных кодов: 8"),
    ).toBeInTheDocument();
    expect(within(petr).getByText("Активных входов").nextElementSibling).toHaveTextContent("3");
    const companies = within(petr).getByRole("list", { name: "Компании: Пётр Орлов" });
    expect(companies).toHaveTextContent("ООО «Меридиан Строй» · администратор · активен");
    expect(companies).toHaveTextContent("АО «Север» · сотрудник · доступ закрыт");
    expect(within(petr).queryByText("почта не подтверждена")).not.toBeInTheDocument();

    const olga = screen.getByRole("listitem", { name: "olga@sever.ru" });
    expect(within(olga).getByText("почта не подтверждена")).toBeInTheDocument();
    expect(within(olga).getByText("временный пароль")).toBeInTheDocument();
    expect(within(olga).getByText("команда kronto")).toBeInTheDocument();
    expect(within(olga).getByText("только код на почту")).toBeInTheDocument();
    expect(within(olga).getByText("Ни в одной компании не состоит.")).toBeInTheDocument();
  });

  it("без совпадений — короткий ответ", async () => {
    const user = userEvent.setup();
    shell([]);
    renderApp("/staff/people");

    await user.type(
      await screen.findByRole("searchbox", { name: "Почта или имя" }),
      "никто{Enter}",
    );
    expect(await screen.findByText("Никого не нашли по запросу «никто».")).toBeInTheDocument();
  });

  it("ссылку на новый пароль шлёт после подтверждения; на лимит отвечает понятно", async () => {
    const user = userEvent.setup();
    const { resets } = shell([person()]);
    renderApp("/staff/people");

    await user.type(await screen.findByRole("searchbox", { name: "Почта или имя" }), "petr{Enter}");
    const card = await screen.findByRole("listitem", { name: "Пётр Орлов" });
    await user.click(
      within(card).getByRole("button", { name: "Отправить ссылку на новый пароль" }),
    );

    let dialog = screen.getByRole("dialog", { name: "Отправить ссылку на новый пароль?" });
    expect(dialog).toHaveAccessibleDescription(/На petr@meridian-stroy\.ru придёт письмо/);
    expect(dialog).toHaveAccessibleDescription(/команда его не увидит/);
    await user.click(within(dialog).getByRole("button", { name: "Отправить" }));

    expect(
      await screen.findByText("Ссылка на новый пароль отправлена на petr@meridian-stroy.ru"),
    ).toBeInTheDocument();
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(resets).toEqual(["a-1"]);

    server.use(
      http.post("/api/v1/staff/people/:accountId/password-reset", () =>
        HttpResponse.json({ detail: "Слишком много запросов" }, { status: 429 }),
      ),
    );
    await user.click(
      within(card).getByRole("button", { name: "Отправить ссылку на новый пароль" }),
    );
    dialog = screen.getByRole("dialog", { name: "Отправить ссылку на новый пароль?" });
    await user.click(within(dialog).getByRole("button", { name: "Отправить" }));
    expect(await within(dialog).findByRole("alert")).toHaveTextContent(
      "Слишком много писем за час — попробуйте позже",
    );
  });
});
