import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { describe, expect, it } from "vitest";

import type { Schemas } from "../api/client";
import { adminMe, loneMe, me } from "../test/fixtures";
import { renderApp } from "../test/render";
import { server } from "../test/server";

type Support = Schemas["SupportResponse"];

function request(overrides: Partial<Support> = {}): Support {
  return {
    id: "1a2b3c4d-0000-4000-8000-000000000001",
    topic: "answers",
    message: "Ассистент не нашёл приказ об отпуске",
    status: "new",
    created_at: "2026-10-04T09:00:00Z",
    ...overrides,
  };
}

function signedIn(user: Schemas["MeResponse"] = me(), mine: Support[] = []) {
  server.use(
    http.get("/api/v1/auth/me", () => HttpResponse.json(user)),
    http.get("/api/v1/notifications", () => HttpResponse.json({ items: [], unread: 0 })),
    http.get("/api/v1/usage", () => HttpResponse.json({ warning: false })),
    http.get("/api/v1/support/mine", () => HttpResponse.json(mine)),
  );
}

const article = (name: string) => screen.queryByRole("button", { name });

describe("статьи", () => {
  it("делит статьи на сотрудникам и администраторам, открывает и закрывает", async () => {
    signedIn();
    renderApp("/help");
    expect(await screen.findByRole("heading", { level: 1, name: "Помощь" })).toBeInTheDocument();
    expect(document.title).toBe("Помощь — kronto");

    const employees = screen.getByRole("region", { name: "Сотрудникам" });
    const admins = screen.getByRole("region", { name: "Администраторам" });
    expect(
      within(employees).getByRole("button", { name: "Как задать вопрос и уточнить ответ" }),
    ).toBeInTheDocument();
    expect(
      within(admins).getByRole("button", { name: "Пригласить сотрудников и одобрить заявки" }),
    ).toBeInTheDocument();
    expect(
      within(admins).getByText("Разделы «Управления» видны только администраторам компании."),
    ).toBeInTheDocument();

    const toggle = within(employees).getByRole("button", {
      name: "Как приложить файл к вопросу",
    });
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    expect(screen.getByText("До 5 файлов к вопросу, каждый — до 10 МБ.")).not.toBeVisible();

    const user = userEvent.setup();
    await user.click(toggle);
    expect(toggle).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByText("До 5 файлов к вопросу, каждый — до 10 МБ.")).toBeVisible();

    await user.click(toggle);
    expect(toggle).toHaveAttribute("aria-expanded", "false");
  });

  it("ссылки в статьях ведут в разделы приложения", async () => {
    signedIn(adminMe());
    renderApp("/help#admin-documents");
    const body = await screen.findByText(/Документы — в/);
    expect(
      within(body).getByRole("link", { name: "Управление → Источники → Файлы" }),
    ).toHaveAttribute("href", "/admin/sources/files");
  });

  it("поиск по заголовку и тексту, в любой форме слова", async () => {
    signedIn();
    renderApp("/help");
    const user = userEvent.setup();
    const search = await screen.findByRole("searchbox", { name: "Поиск по статьям" });

    // «файлы» — по тексту и заголовку статьи о вложениях, о документах админа.
    await user.type(search, "файлы");
    expect(article("Как приложить файл к вопросу")).toBeInTheDocument();
    expect(article("Загрузить документы и настроить доступ по отделам")).toBeInTheDocument();
    expect(article("Как поделиться диалогом")).not.toBeInTheDocument();

    // Только по тексту статьи и нескольким словам сразу.
    await user.clear(search);
    await user.type(search, "Яндекс Ключ");
    expect(article("Второй фактор и ключи доступа")).toBeInTheDocument();
    expect(article("Как подключить свой Битрикс24 или Яндекс 360")).not.toBeInTheDocument();
    expect(screen.queryByRole("region", { name: "Администраторам" })).not.toBeInTheDocument();
    expect(screen.getByText("Найдено 1 статья")).toBeInTheDocument();

    await user.clear(search);
    await user.type(search, "телепорт");
    expect(screen.getByText("Ничего не нашлось")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "напишите в поддержку" })).toHaveAttribute(
      "href",
      "/help#support",
    );

    await user.clear(search);
    expect(article("Как поделиться диалогом")).toBeInTheDocument();
  });

  it("открывает статью из адреса #slug", async () => {
    signedIn();
    renderApp("/help#share");
    const toggle = await screen.findByRole("button", { name: "Как поделиться диалогом" });
    expect(toggle).toHaveAttribute("aria-expanded", "true");
    expect(toggle).toHaveFocus();
    expect(screen.getByText(/Ссылку откроют только коллеги/)).toBeVisible();
    expect(
      screen.getByRole("button", { name: "Как задать вопрос и уточнить ответ" }),
    ).toHaveAttribute("aria-expanded", "false");
  });

  it("ссылка на другую статью открывает её и сбрасывает мешающий поиск", async () => {
    signedIn(adminMe());
    renderApp("/help");
    const user = userEvent.setup();
    const search = await screen.findByRole("searchbox", { name: "Поиск по статьям" });
    await user.type(search, "приглашение одобрение");
    await user.click(
      screen.getByRole("button", { name: "Пригласить сотрудников и одобрить заявки" }),
    );
    await user.click(screen.getByRole("link", { name: "«Тарифе»" }));

    const tariff = await screen.findByRole("button", { name: "Лимит вопросов и тариф" });
    expect(tariff).toHaveAttribute("aria-expanded", "true");
    expect(search).toHaveValue("");
  });
});

describe("написать в поддержку", () => {
  it("проверяет тему и текст до отправки", async () => {
    signedIn();
    let posted = 0;
    server.use(
      http.post("/api/v1/support", () => {
        posted += 1;
        return HttpResponse.json(request(), { status: 201 });
      }),
    );
    renderApp("/help");
    const user = userEvent.setup();
    const form = await screen.findByRole("region", { name: "Написать в поддержку" });
    expect(
      within(form).getByText(
        "Ответим на почту anna@meridian-stroy.ru. Пароли и коды из писем не присылайте.",
      ),
    ).toBeInTheDocument();

    await user.click(within(form).getByRole("button", { name: "Отправить" }));
    expect(within(form).getByText("Выберите тему.")).toBeInTheDocument();
    expect(within(form).getByText("Опишите, что случилось.")).toBeInTheDocument();
    expect(within(form).getByLabelText(/Сообщение/)).toHaveAttribute("aria-invalid", "true");

    await user.selectOptions(within(form).getByLabelText(/Тема/), "Другое");
    await user.type(within(form).getByLabelText(/Сообщение/), "   не работ   ");
    expect(within(form).queryByText("Выберите тему.")).not.toBeInTheDocument();
    expect(
      within(form).getByText("Напишите чуть подробнее — хотя бы 10 символов."),
    ).toBeInTheDocument();
    await user.click(within(form).getByRole("button", { name: "Отправить" }));
    expect(posted).toBe(0);
  });

  it("отправляет обращение, показывает номер и добавляет его в «Ваши обращения»", async () => {
    signedIn();
    let body: unknown;
    server.use(
      http.post("/api/v1/support", async ({ request: req }) => {
        body = await req.json();
        return HttpResponse.json(request(), { status: 201 });
      }),
    );
    renderApp("/help");
    const user = userEvent.setup();
    const form = await screen.findByRole("region", { name: "Написать в поддержку" });
    const message = within(form).getByLabelText(/Сообщение/);

    await user.selectOptions(within(form).getByLabelText(/Тема/), "Ответы ассистента");
    await user.type(message, "  Ассистент не нашёл приказ об отпуске  ");
    expect(within(form).getByText(/40\s\/\s4\s000/)).toBeInTheDocument();
    await user.click(within(form).getByRole("button", { name: "Отправить" }));

    expect(await within(form).findByText("Обращение №1a2b3c4d отправлено")).toBeInTheDocument();
    expect(within(form).getByText("Ответим на почту anna@meridian-stroy.ru.")).toBeInTheDocument();
    expect(body).toEqual({ topic: "answers", message: "Ассистент не нашёл приказ об отпуске" });
    expect(message).toHaveValue("");
    expect(within(form).getByLabelText(/Тема/)).toHaveValue("");

    const mine = within(form).getByRole("region", { name: "Ваши обращения" });
    expect(within(mine).getByText("Ответы ассистента")).toBeInTheDocument();
    expect(within(mine).getByText("новое")).toBeInTheDocument();
    expect(within(mine).getByText(/№1a2b3c4d/)).toBeInTheDocument();
  });

  it("слишком много обращений за час — просит попробовать позже", async () => {
    signedIn();
    server.use(
      http.post("/api/v1/support", () =>
        HttpResponse.json({ detail: "Слишком много запросов" }, { status: 429 }),
      ),
    );
    renderApp("/help");
    const user = userEvent.setup();
    const form = await screen.findByRole("region", { name: "Написать в поддержку" });
    await user.selectOptions(within(form).getByLabelText(/Тема/), "Вход и учётная запись");
    await user.type(within(form).getByLabelText(/Сообщение/), "Не приходит код на почту");
    await user.click(within(form).getByRole("button", { name: "Отправить" }));

    expect(
      await within(form).findByText(
        "Вы уже отправили несколько обращений за час. Попробуйте позже.",
      ),
    ).toBeInTheDocument();
    // Текст не пропал — можно отправить позже.
    expect(within(form).getByLabelText(/Сообщение/)).toHaveValue("Не приходит код на почту");
  });

  it("показывает свои обращения с темой, датой и состоянием", async () => {
    signedIn(me(), [
      request(),
      request({
        id: "5e6f7a8b-0000-4000-8000-000000000002",
        topic: "billing",
        message: "Как добавить места?",
        status: "answered",
      }),
      request({
        id: "9c0d1e2f-0000-4000-8000-000000000003",
        topic: "login",
        message: "Не могу войти",
        status: "closed",
      }),
    ]);
    renderApp("/help");
    const mine = await screen.findByRole("region", { name: "Ваши обращения" });
    const items = within(mine).getAllByRole("listitem");
    expect(items).toHaveLength(3);
    expect(items[0]).toHaveTextContent("Ответы ассистента");
    expect(items[0]).toHaveTextContent("новое");
    expect(items[0]).toHaveTextContent("№1a2b3c4d");
    expect(items[1]).toHaveTextContent("Тариф и оплата");
    expect(items[1]).toHaveTextContent("отвечено");
    expect(items[1]).toHaveTextContent("Как добавить места?");
    expect(items[2]).toHaveTextContent("Вход и учётная запись");
    expect(items[2]).toHaveTextContent("закрыто");
    expect(within(mine).getByText("Ответы приходят на почту.")).toBeInTheDocument();
  });

  it("обращений нет — списка нет", async () => {
    signedIn();
    renderApp("/help");
    await screen.findByRole("region", { name: "Написать в поддержку" });
    await waitFor(() =>
      expect(screen.queryByRole("region", { name: "Ваши обращения" })).not.toBeInTheDocument(),
    );
  });

  it("кнопка в шапке ведёт к форме", async () => {
    signedIn();
    renderApp("/help");
    const user = userEvent.setup();
    await user.click(await screen.findByRole("link", { name: "Написать в поддержку" }));
    expect(screen.getByRole("heading", { name: "Написать в поддержку" })).toHaveFocus();
  });
});

describe("без компании", () => {
  it("помощь и поддержка работают", async () => {
    signedIn(loneMe({ email: "ivan@example.ru" }));
    let body: unknown;
    server.use(
      http.post("/api/v1/support", async ({ request: req }) => {
        body = await req.json();
        return HttpResponse.json(request({ topic: "login" }), { status: 201 });
      }),
    );
    renderApp("/help");
    const user = userEvent.setup();

    expect(
      await screen.findByRole("button", { name: "Как вступить в компанию" }),
    ).toBeInTheDocument();
    const form = screen.getByRole("region", { name: "Написать в поддержку" });
    expect(within(form).getByText(/Ответим на почту ivan@example.ru\./)).toBeInTheDocument();

    await user.selectOptions(within(form).getByLabelText(/Тема/), "Вход и учётная запись");
    await user.type(within(form).getByLabelText(/Сообщение/), "Не пришло письмо с кодом");
    await user.click(within(form).getByRole("button", { name: "Отправить" }));
    expect(await within(form).findByText("Обращение №1a2b3c4d отправлено")).toBeInTheDocument();
    expect(body).toEqual({ topic: "login", message: "Не пришло письмо с кодом" });
  });
});
