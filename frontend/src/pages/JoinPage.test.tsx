import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { describe, expect, it } from "vitest";

import { getSession } from "../api/session";
import { me, tokens } from "../test/fixtures";
import { renderApp } from "../test/render";
import { server } from "../test/server";

const TOKEN = "tok_0123456789abcdefghijklmnopqrstuvwxyz0123";

function preview() {
  server.use(
    http.post("/api/v1/invites/preview", () =>
      HttpResponse.json({
        company_name: "ООО «Меридиан Строй»",
        expires_at: "2026-10-05T12:00:00+03:00",
        email_domain: "meridian-stroy.ru",
      }),
    ),
  );
}

describe("присоединение по ссылке-приглашению", () => {
  it("карточка «Присоединиться», затем учётка и сразу вход", async () => {
    const user = userEvent.setup();
    preview();
    let previewBody: unknown;
    let acceptBody: unknown;
    server.use(
      http.post("/api/v1/invites/preview", async ({ request }) => {
        previewBody = await request.json();
        return HttpResponse.json({
          company_name: "ООО «Меридиан Строй»",
          expires_at: "2026-10-05T12:00:00+03:00",
          email_domain: "meridian-stroy.ru",
        });
      }),
      http.post("/api/v1/invites/accept", async ({ request }) => {
        acceptBody = await request.json();
        return HttpResponse.json(tokens(7), { status: 201 });
      }),
      http.get("/api/v1/auth/me", () => HttpResponse.json(me())),
    );
    renderApp(`/join/meridian#${TOKEN}`, { signedIn: false });

    expect(
      await screen.findByRole("heading", { name: "Присоединиться к команде" }),
    ).toBeInTheDocument();
    expect(previewBody).toEqual({ company_code: "meridian", token: TOKEN });
    await user.click(screen.getByRole("button", { name: "Присоединиться" }));

    expect(screen.getByText("Только почта @meridian-stroy.ru.")).toBeInTheDocument();
    await user.type(screen.getByLabelText("Рабочая почта"), "anna@meridian-stroy.ru");
    await user.type(screen.getByLabelText(/Имя и фамилия/), "Анна Смирнова");
    await user.type(screen.getByLabelText("Пароль"), "длинная фраза для входа");
    await user.type(screen.getByLabelText("Повторите пароль"), "длинная фраза для входа");
    await user.click(screen.getByRole("button", { name: "Присоединиться" }));

    expect(await screen.findByRole("log", { name: "Переписка" })).toBeInTheDocument();
    expect(acceptBody).toEqual({
      company_code: "meridian",
      token: TOKEN,
      email: "anna@meridian-stroy.ru",
      full_name: "Анна Смирнова",
      password: "длинная фраза для входа",
    });
    expect(getSession()?.accessToken).toBe("access-7");
  });

  it("несовпадающие пароли не уходят на сервер", async () => {
    const user = userEvent.setup();
    preview();
    let accepted = false;
    server.use(
      http.post("/api/v1/invites/accept", () => {
        accepted = true;
        return HttpResponse.json(tokens(1), { status: 201 });
      }),
    );
    renderApp(`/join/meridian#${TOKEN}`, { signedIn: false });
    await user.click(await screen.findByRole("button", { name: "Присоединиться" }));
    await user.type(screen.getByLabelText("Рабочая почта"), "anna@meridian-stroy.ru");
    await user.type(screen.getByLabelText("Пароль"), "длинная фраза для входа");
    await user.type(screen.getByLabelText("Повторите пароль"), "другая фраза для входа");
    await user.click(screen.getByRole("button", { name: "Присоединиться" }));

    expect(await screen.findByText("Пароли не совпадают.")).toBeInTheDocument();
    expect(accepted).toBe(false);
  });

  it("недействительная ссылка — понятное сообщение", async () => {
    server.use(
      http.post("/api/v1/invites/preview", () =>
        HttpResponse.json(
          { detail: "Ссылка-приглашение недействительна или истекла." },
          { status: 404 },
        ),
      ),
    );
    renderApp(`/join/meridian#${TOKEN}`, { signedIn: false });

    expect(await screen.findByRole("alert")).toHaveTextContent("недействительна или истекла");
  });

  it("ссылка без токена", async () => {
    renderApp("/join/meridian", { signedIn: false });

    expect(await screen.findByRole("alert")).toHaveTextContent("В ссылке нет кода приглашения");
  });

  it("вошедший в эту же компанию сразу попадает к вопросам", async () => {
    preview();
    server.use(http.get("/api/v1/auth/me", () => HttpResponse.json(me())));
    renderApp(`/join/meridian#${TOKEN}`);

    expect(await screen.findByRole("log", { name: "Переписка" })).toBeInTheDocument();
  });

  it("вошедший в другую компанию может выйти и присоединиться", async () => {
    const user = userEvent.setup();
    preview();
    server.use(
      http.get("/api/v1/auth/me", () =>
        HttpResponse.json(me({ company_code: "other", company_name: "Другая" })),
      ),
      http.post("/api/v1/auth/logout", () => new HttpResponse(null, { status: 204 })),
    );
    renderApp(`/join/meridian#${TOKEN}`);

    await user.click(await screen.findByRole("button", { name: "Выйти и продолжить" }));

    expect(
      await screen.findByRole("heading", { name: "Присоединиться к команде" }),
    ).toBeInTheDocument();
    await waitFor(() => expect(getSession()).toBeNull());
  });
});
