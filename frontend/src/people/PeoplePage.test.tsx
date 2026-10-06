import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { describe, expect, it } from "vitest";

import type { Schemas } from "../api/client";
import { adminMe, me } from "../test/fixtures";
import { renderApp } from "../test/render";
import { server } from "../test/server";

type Person = Schemas["PersonResponse"];

function person(id: string, overrides: Partial<Person> = {}): Person {
  return {
    member_id: id,
    first_name: null,
    last_name: null,
    patronymic: null,
    full_name: null,
    email: `${id}@meridian-stroy.ru`,
    phone: null,
    telegram: null,
    avatar_url: null,
    position: null,
    department: null,
    department_confirmed: Boolean(overrides.department),
    role: "employee",
    ...overrides,
  };
}

const SALES = { id: "d-1", name: "Продажи" };
const PEOPLE = [
  person("m-1", {
    first_name: "Анна",
    last_name: "Смирнова",
    full_name: "Анна Смирнова",
    email: "anna@meridian-stroy.ru",
  }),
  person("m-2", {
    first_name: "Семён",
    last_name: "Ёлкин",
    patronymic: "Петрович",
    full_name: "Семён Ёлкин",
    position: "Менеджер по продажам",
    department: SALES,
    phone: "+79991234567",
    telegram: "semen_e",
    role: "admin",
  }),
  person("m-3", { first_name: "Ольга", last_name: "Иванова", full_name: "Ольга Иванова" }),
];

function directory(profile = me()) {
  server.use(
    http.get("/api/v1/auth/me", () => HttpResponse.json(profile)),
    http.get("/api/v1/people", () => HttpResponse.json(PEOPLE)),
    http.get("/api/v1/usage", () => HttpResponse.json({ warning: false })),
    http.get("/api/v1/departments", () =>
      HttpResponse.json([{ ...SALES, members: 1, unconfirmed: 0 }]),
    ),
  );
}

describe("справочник коллег", () => {
  it("ищет без учёта регистра и «ё», отбирает по отделу", async () => {
    const user = userEvent.setup();
    directory();
    renderApp("/people");

    const list = await screen.findByRole("list", { name: "Коллеги" });
    expect(within(list).getAllByRole("listitem")).toHaveLength(3);
    expect(screen.getByRole("heading", { name: "Коллеги" })).toBeInTheDocument();
    expect(document.title).toBe("Коллеги — kronto");
    expect(screen.getByText(/3 человека в «ООО «Меридиан Строй»»/)).toBeInTheDocument();

    await user.type(
      screen.getByRole("searchbox", { name: "Поиск по имени, должности, отделу" }),
      "семен елкин",
    );
    expect(
      within(screen.getByRole("list", { name: "Коллеги" })).getAllByRole("listitem"),
    ).toHaveLength(1);
    expect(screen.getByText("Менеджер по продажам")).toBeInTheDocument();

    await user.clear(screen.getByRole("searchbox", { name: "Поиск по имени, должности, отделу" }));
    await user.selectOptions(screen.getByRole("combobox", { name: "Отдел" }), "none");
    expect(
      within(screen.getByRole("list", { name: "Коллеги" })).getAllByRole("listitem"),
    ).toHaveLength(2);

    await user.type(
      screen.getByRole("searchbox", { name: "Поиск по имени, должности, отделу" }),
      "нет такого",
    );
    expect(screen.getByText("Никого не нашли")).toBeInTheDocument();
  });

  it("карточка: полное имя, контакты ссылками, роль", async () => {
    const user = userEvent.setup();
    directory();
    renderApp("/people");

    const list = await screen.findByRole("list", { name: "Коллеги" });
    await user.click(within(list).getByRole("button", { name: /Семён Ёлкин/ }));
    const card = screen.getByRole("dialog", { name: "Ёлкин Семён Петрович" });
    expect(within(card).getByText("Менеджер по продажам · Продажи")).toBeInTheDocument();
    expect(within(card).getByText("администратор")).toBeInTheDocument();
    expect(within(card).getByRole("link", { name: "m-2@meridian-stroy.ru" })).toHaveAttribute(
      "href",
      "mailto:m-2@meridian-stroy.ru",
    );
    expect(within(card).getByRole("link", { name: "+7 999 123-45-67" })).toHaveAttribute(
      "href",
      "tel:+79991234567",
    );
    expect(within(card).getByRole("link", { name: "@semen_e" })).toHaveAttribute(
      "href",
      "https://t.me/semen_e",
    );
    // Сотрудник чужую должность не правит.
    expect(within(card).queryByRole("button", { name: /Поправить/ })).not.toBeInTheDocument();
  });

  it("своя карточка ведёт в настройки профиля", async () => {
    const user = userEvent.setup();
    directory();
    renderApp("/people");

    const list = await screen.findByRole("list", { name: "Коллеги" });
    await user.click(within(list).getByRole("button", { name: /Анна Смирнова/ }));
    const card = screen.getByRole("dialog", { name: "Смирнова Анна" });
    expect(within(card).getByText("это вы")).toBeInTheDocument();
    expect(within(card).getByRole("link", { name: "Изменить свой профиль" })).toHaveAttribute(
      "href",
      "/settings/profile",
    );
  });

  it("администратор поправляет должность и отдел коллеги", async () => {
    const user = userEvent.setup();
    let body: unknown;
    directory(adminMe());
    server.use(
      http.patch("/api/v1/people/:memberId", async ({ request, params }) => {
        body = { memberId: params.memberId, ...((await request.json()) as object) };
        return HttpResponse.json(PEOPLE[2]);
      }),
    );
    renderApp("/people");

    const list = await screen.findByRole("list", { name: "Коллеги" });
    await user.click(within(list).getByRole("button", { name: /Ольга Иванова/ }));
    await user.click(screen.getByRole("button", { name: "Поправить должность и отдел" }));
    const dialog = screen.getByRole("dialog", { name: "Должность и отдел" });
    await user.type(within(dialog).getByLabelText(/^Должность/), "Бухгалтер");
    await user.selectOptions(await within(dialog).findByLabelText(/^Отдел/), "d-1");
    await user.click(within(dialog).getByRole("button", { name: "Сохранить" }));

    expect(await screen.findByText("Сохранено")).toBeInTheDocument();
    expect(body).toEqual({ memberId: "m-3", position: "Бухгалтер", department_id: "d-1" });
  });
});
