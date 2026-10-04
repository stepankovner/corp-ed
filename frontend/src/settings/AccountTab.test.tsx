import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { describe, expect, it } from "vitest";

import { getSession } from "../api/session";
import { me } from "../test/fixtures";
import { renderApp } from "../test/render";
import { server } from "../test/server";

function signedIn() {
  server.use(http.get("/api/v1/auth/me", () => HttpResponse.json(me())));
}

describe("управление учётной записью: почта", () => {
  it("без приложения: код на прежний адрес, затем ссылка на новый", async () => {
    const user = userEvent.setup();
    const bodies: unknown[] = [];
    signedIn();
    server.use(
      http.post("/api/v1/account/email", async ({ request }) => {
        const body = (await request.json()) as { code: string | null };
        bodies.push(body);
        return HttpResponse.json(
          body.code
            ? { status: "link_sent", email_hint: null }
            : { status: "code_sent", email_hint: "a***@meridian-stroy.ru" },
          { status: 202 },
        );
      }),
    );
    renderApp("/settings/account");

    const section = await screen.findByRole("region", { name: "Почта" });
    expect(document.title).toBe("Управление учётной записью — kronto");
    expect(within(section).getByText("anna@meridian-stroy.ru")).toBeInTheDocument();
    await user.type(within(section).getByLabelText("Новая почта"), " anna@sever.ru ");
    await user.type(within(section).getByLabelText("Пароль от учётной записи"), "мой пароль");
    await user.click(within(section).getByRole("button", { name: "Получить код" }));

    expect(
      await within(section).findByText(/Отправили код на a\*\*\*@meridian-stroy\.ru/),
    ).toBeInTheDocument();
    await user.type(within(section).getByLabelText("Код из письма"), "123456");
    await user.click(within(section).getByRole("button", { name: "Подтвердить" }));

    expect(
      await within(section).findByText(
        /Мы отправили ссылку на anna@sever\.ru; почта сменится после перехода по ней/,
      ),
    ).toBeInTheDocument();
    expect(bodies).toEqual([
      { new_email: "anna@sever.ru", password: "мой пароль", code: null },
      { new_email: "anna@sever.ru", password: "мой пароль", code: "123456" },
    ]);
    expect(within(section).getByLabelText("Пароль от учётной записи")).toHaveValue("");
    expect(within(section).getByLabelText("Новая почта")).toHaveValue("");
  });

  it("с приложением: код приложения в той же форме", async () => {
    const user = userEvent.setup();
    let body: unknown;
    server.use(
      http.get("/api/v1/auth/me", () =>
        HttpResponse.json(me({ mfa: { strong: true, strong_required: false } })),
      ),
      http.post("/api/v1/account/email", async ({ request }) => {
        body = await request.json();
        return HttpResponse.json({ status: "link_sent", email_hint: null }, { status: 202 });
      }),
    );
    renderApp("/settings/account");

    const section = await screen.findByRole("region", { name: "Почта" });
    await user.type(within(section).getByLabelText("Новая почта"), "anna@sever.ru");
    await user.type(within(section).getByLabelText("Пароль от учётной записи"), "мой пароль");
    await user.type(
      within(section).getByLabelText("Код из приложения или резервный код"),
      "654321",
    );
    await user.click(within(section).getByRole("button", { name: "Сменить почту" }));

    expect(
      await within(section).findByText(/Мы отправили ссылку на anna@sever\.ru/),
    ).toBeInTheDocument();
    expect(body).toEqual({ new_email: "anna@sever.ru", password: "мой пароль", code: "654321" });
  });

  it("занятая почта — ошибка у поля адреса", async () => {
    const user = userEvent.setup();
    signedIn();
    server.use(
      http.post("/api/v1/account/email", () =>
        HttpResponse.json(
          { detail: "Эта почта уже используется другой учётной записью", code: "email_taken" },
          { status: 409 },
        ),
      ),
    );
    renderApp("/settings/account");

    const section = await screen.findByRole("region", { name: "Почта" });
    await user.type(within(section).getByLabelText("Новая почта"), "pavel@meridian-stroy.ru");
    await user.type(within(section).getByLabelText("Пароль от учётной записи"), "мой пароль");
    await user.click(within(section).getByRole("button", { name: "Получить код" }));

    const field = within(section).getByLabelText("Новая почта");
    await waitFor(() => expect(field).toHaveAttribute("aria-invalid", "true"));
    expect(field).toHaveAccessibleDescription("Эта почта уже используется другой учётной записью");
    expect(within(section).queryByText(/Мы отправили ссылку/)).not.toBeInTheDocument();
  });
});

describe("управление учётной записью: удаление", () => {
  it("после пароля удаляет учётку и выходит на страницу входа", async () => {
    const user = userEvent.setup();
    let body: unknown;
    let loggedOut = false;
    signedIn();
    server.use(
      http.post("/api/v1/account/delete", async ({ request }) => {
        body = await request.json();
        return new HttpResponse(null, { status: 204 });
      }),
      http.post("/api/v1/auth/logout", () => {
        loggedOut = true;
        return new HttpResponse(null, { status: 204 });
      }),
    );
    const { router } = renderApp("/settings/account");

    const section = await screen.findByRole("region", { name: "Удаление учётной записи" });
    expect(within(section).getByText(/удалятся через 30 дней/)).toBeInTheDocument();
    await user.click(within(section).getByRole("button", { name: "Удалить учётную запись" }));
    const dialog = await screen.findByRole("dialog", { name: "Удалить учётную запись?" });
    const confirm = within(dialog).getByRole("button", { name: "Удалить навсегда" });
    expect(confirm).toBeDisabled();
    await user.type(within(dialog).getByLabelText("Пароль от учётной записи"), "мой пароль");
    await user.click(confirm);

    await waitFor(() => expect(router.state.location.pathname).toBe("/login"));
    expect(body).toEqual({ password: "мой пароль" });
    expect(loggedOut).toBe(true);
    expect(getSession()).toBeNull();
    expect(await screen.findByText("Учётная запись удалена")).toBeInTheDocument();
  });

  it("единственному администратору — объяснение сервера, учётка остаётся", async () => {
    const user = userEvent.setup();
    signedIn();
    server.use(
      http.post("/api/v1/account/delete", () =>
        HttpResponse.json(
          {
            detail:
              "Вы единственный администратор: «ООО «Меридиан Строй»». Назначьте другого администратора, затем удалите учётку",
            code: "last_admin",
          },
          { status: 409 },
        ),
      ),
    );
    const { router } = renderApp("/settings/account");

    await user.click(await screen.findByRole("button", { name: "Удалить учётную запись" }));
    const dialog = await screen.findByRole("dialog", { name: "Удалить учётную запись?" });
    await user.type(within(dialog).getByLabelText("Пароль от учётной записи"), "мой пароль");
    await user.click(within(dialog).getByRole("button", { name: "Удалить навсегда" }));

    expect(
      await within(dialog).findByText(/Вы единственный администратор: «ООО «Меридиан Строй»»/),
    ).toBeInTheDocument();
    expect(within(dialog).getByLabelText("Пароль от учётной записи")).toHaveValue("");
    expect(router.state.location.pathname).toBe("/settings/account");
    expect(getSession()).not.toBeNull();
  });
});
