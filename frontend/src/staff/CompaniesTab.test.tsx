import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { Schemas } from "../api/client";
import { adminMe } from "../test/fixtures";
import { renderApp } from "../test/render";
import { server } from "../test/server";

type Company = Schemas["StaffCompanyResponse"];
type Update = Partial<Schemas["StaffCompanyUpdate"]>;

const CREDITS_PER_SEAT = 420;

function company(overrides: Partial<Company> = {}): Company {
  return {
    // Компания, в которой сейчас сам сотрудник kronto (adminMe — t-1).
    id: "t-1",
    ref: "1a2b3c4d",
    name: "Меридиан Строй",
    company_code: "meridian-1a2b",
    is_active: true,
    data_deleted_at: null,
    tariff: "base",
    seats: 30,
    pilot_until: "2026-10-08",
    members: 12,
    pending: 2,
    admins: ["anna@meridian.ru"],
    credits_used: 3150,
    pool: 30 * CREDITS_PER_SEAT,
    purchased_credits: 0,
    questions_month: 840,
    last_question_at: "2026-10-04T07:00:00Z",
    documents: 48,
    connectors: 2,
    ...overrides,
  };
}

const SEVER = company({
  id: "t-2",
  ref: "9f00aa11",
  name: "Северный ветер",
  company_code: "sever-9f00",
  is_active: false,
  tariff: "extended",
  seats: 10,
  members: 10,
  pending: 0,
  pilot_until: "2026-09-30",
  admins: [],
  credits_used: 4200,
  pool: 10 * CREDITS_PER_SEAT,
  last_question_at: null,
  questions_month: 0,
});

const VOSTOK = company({
  id: "t-3",
  ref: "77aa0c3e",
  name: "Восток",
  company_code: "vostok-77aa",
  pilot_until: null,
  pending: 0,
  admins: ["olga@vostok.ru", "petr@vostok.ru"],
});

/**
 * Сервер панели в памяти. Места, при которых пул меньше потраченного, без
 * confirm — 409 seats_stop_pool, как на бэкенде; bodies — тела PATCH.
 */
function mockStaff(initial: Company[] = [company(), SEVER, VOSTOK]) {
  let companies = initial;
  const bodies: Update[] = [];
  const deletions: unknown[] = [];
  server.use(
    http.get("/api/v1/auth/me", () => HttpResponse.json(adminMe({ staff: true }))),
    http.get("/api/v1/usage", () => HttpResponse.json({ warning: false })),
    http.get("/api/v1/staff/overview", () =>
      HttpResponse.json({
        companies: companies.length,
        active_companies: companies.filter((c) => c.is_active).length,
        pilots_ending: 2,
        requests_new: 0,
        accounts: 40,
      }),
    ),
    http.get("/api/v1/staff/companies", () => HttpResponse.json(companies)),
    http.patch("/api/v1/staff/companies/:id", async ({ request, params }) => {
      const body = (await request.json()) as Update;
      bodies.push(body);
      const current = companies.find((c) => c.id === params.id)!;
      const pool = (body.seats ?? current.seats) * CREDITS_PER_SEAT;
      if (body.seats != null && pool <= current.credits_used && !body.confirm) {
        return HttpResponse.json(
          {
            detail: `за месяц потрачено ${current.credits_used} кредитов, новый пул — ${pool} (${body.seats} × ${CREDITS_PER_SEAT}): вопросы сотрудников остановятся до 01.11.2026.`,
            code: "seats_stop_pool",
          },
          { status: 409 },
        );
      }
      const next: Company = {
        ...current,
        ...(body.tariff ? { tariff: body.tariff } : {}),
        ...(body.seats ? { seats: body.seats, pool } : {}),
        ...("pilot_until" in body ? { pilot_until: body.pilot_until ?? null } : {}),
        ...(body.is_active != null ? { is_active: body.is_active } : {}),
      };
      companies = companies.map((c) => (c.id === next.id ? next : c));
      return HttpResponse.json(next);
    }),
    http.post("/api/v1/staff/companies/:id/delete-data", async ({ request, params }) => {
      const body = (await request.json()) as { company_code: string };
      deletions.push(body);
      const current = companies.find((c) => c.id === params.id)!;
      if (body.company_code !== current.company_code) {
        return HttpResponse.json(
          { detail: "Код компании не совпадает — данные не удалены", code: "code_mismatch" },
          { status: 409 },
        );
      }
      const next: Company = {
        ...current,
        name: `Удалённая компания ${current.ref}`,
        company_code: `deleted-${current.ref}`,
        data_deleted_at: "2026-10-04T09:00:00Z",
        members: 0,
        pending: 0,
        admins: [],
        documents: 0,
        connectors: 0,
        pilot_until: null,
      };
      companies = companies.map((c) => (c.id === next.id ? next : c));
      return HttpResponse.json({ company: next, deleted: { users: 10, materials: 48 } });
    }),
  );
  return { bodies, deletions };
}

function row(name: string): HTMLElement {
  return screen.getByRole("row", { name: new RegExp(name) });
}

async function openEditor(name: string) {
  const user = userEvent.setup();
  await screen.findByRole("table", { name: "Компании" });
  await user.click(within(row(name)).getByRole("button", { name: `Изменить: ${name}` }));
  return { user, dialog: screen.getByRole("dialog", { name }) };
}

beforeEach(() => {
  // Только дата: сроки пилота считаются от «сегодня»; таймеры остаются настоящими.
  vi.useFakeTimers({ toFake: ["Date"], now: new Date("2026-10-04T09:00:00Z") });
});

afterEach(() => {
  vi.useRealTimers();
});

describe("панель: компании", () => {
  it("таблица: места, кредиты, пилот, состояние; поиск по коду и почте", async () => {
    const user = userEvent.setup();
    mockStaff();
    renderApp("/staff/companies");

    await screen.findByRole("table", { name: "Компании" });
    expect(document.title).toBe("Компании — kronto");

    const meridian = row("Меридиан Строй");
    expect(within(meridian).getByText("meridian-1a2b")).toBeInTheDocument();
    // Короткий id — им компания названа в уведомлениях команде в Telegram.
    expect(within(meridian).getByText("1a2b3c4d")).toBeInTheDocument();
    expect(within(meridian).getByText("anna@meridian.ru")).toBeInTheDocument();
    expect(within(meridian).getByText("Базовый")).toBeInTheDocument();
    expect(within(meridian).getByText("12 из 30 мест")).toBeInTheDocument();
    expect(within(meridian).getByText("ждут: 2")).toBeInTheDocument();
    expect(within(meridian).getByText("25 %")).toBeInTheDocument();
    expect(within(meridian).getByText("пилот до 8 октября")).toBeInTheDocument();
    expect(within(meridian).getByText("осталось 4 дня")).toBeInTheDocument();
    expect(within(meridian).getByText("работает")).toBeInTheDocument();
    expect(within(meridian).getByText("2 часа назад")).toBeInTheDocument();

    const sever = row("Северный ветер");
    expect(within(sever).getByText("Расширенный")).toBeInTheDocument();
    expect(within(sever).getByText("администраторов нет")).toBeInTheDocument();
    expect(within(sever).queryByText(/ждут/)).not.toBeInTheDocument();
    expect(within(sever).getByText("100 %")).toBeInTheDocument();
    expect(within(sever).getByText("пилот до 30 сентября")).toBeInTheDocument();
    expect(within(sever).getByText("закончился")).toBeInTheDocument();
    expect(within(sever).getByText("на паузе")).toBeInTheDocument();
    expect(within(sever).getByText("не было")).toBeInTheDocument();

    const vostok = row("Восток");
    expect(within(vostok).queryByText(/пилот|осталось|закончился/)).not.toBeInTheDocument();

    const search = screen.getByRole("searchbox", {
      name: "Поиск по названию, коду, id и почте администратора",
    });
    await user.type(search, "77AA0C");
    expect(screen.getAllByRole("row")).toHaveLength(2);
    expect(row("Восток")).toBeInTheDocument();
    await user.clear(search);
    await user.type(search, "PETR@");
    expect(screen.getAllByRole("row")).toHaveLength(2);
    expect(row("Восток")).toBeInTheDocument();
    await user.clear(search);
    await user.type(search, "sever-9");
    expect(row("Северный ветер")).toBeInTheDocument();
    expect(screen.queryByRole("row", { name: /Восток/ })).not.toBeInTheDocument();
    await user.type(search, "нет такой");
    expect(screen.getByText("Ничего не найдено.")).toBeInTheDocument();
  });

  it("правка уходит только с изменёнными полями", async () => {
    const { bodies } = mockStaff();
    renderApp("/staff/companies");
    const { user, dialog } = await openEditor("Восток");

    expect(within(dialog).getByRole("link", { name: "olga@vostok.ru" })).toHaveAttribute(
      "href",
      "mailto:olga@vostok.ru",
    );
    expect(within(dialog).getByRole("link", { name: "petr@vostok.ru" })).toBeInTheDocument();
    const save = within(dialog).getByRole("button", { name: "Сохранить" });
    expect(save).toBeDisabled();

    await user.selectOptions(within(dialog).getByLabelText("Тариф"), "extended");
    const seats = within(dialog).getByLabelText("Рабочих мест");
    await user.clear(seats);
    await user.type(seats, "40");
    expect(dialog).toHaveTextContent(/Пул станет 16\s800 кредитов в месяц/);
    await user.type(within(dialog).getByLabelText(/Пилот до/), "2026-11-30");
    await user.click(save);

    expect(await screen.findByText("Сохранено: Восток")).toBeInTheDocument();
    expect(bodies).toEqual([{ tariff: "extended", seats: 40, pilot_until: "2026-11-30" }]);
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(await within(row("Восток")).findByText("12 из 40 мест")).toBeInTheDocument();
    expect(within(row("Восток")).getByText("Расширенный")).toBeInTheDocument();
  });

  it("места, при которых пул меньше потраченного, — только с явным согласием", async () => {
    const { bodies } = mockStaff();
    renderApp("/staff/companies");
    const { user, dialog } = await openEditor("Меридиан Строй");

    const seats = within(dialog).getByLabelText("Рабочих мест");
    await user.clear(seats);
    await user.type(seats, "5");
    // Сотрудников больше, чем мест, — предупреждаем ещё до сервера.
    expect(within(dialog).getByText(/Мест меньше, чем сотрудников/)).toBeInTheDocument();
    const save = within(dialog).getByRole("button", { name: "Сохранить" });
    await user.click(save);

    const notice = await within(dialog).findByText(/За месяц потрачено 3150 кредитов/);
    expect(notice).toHaveTextContent("вопросы сотрудников остановятся до 01.11.2026");
    expect(save).toBeDisabled();
    const agree = within(dialog).getByRole("checkbox", {
      name: "Всё равно сохранить — вопросы остановятся до конца месяца",
    });
    await user.click(agree);
    expect(save).toBeEnabled();
    await user.click(save);

    expect(await screen.findByText("Сохранено: Меридиан Строй")).toBeInTheDocument();
    expect(bodies).toEqual([{ seats: 5 }, { seats: 5, confirm: true }]);
  });

  it("другое число мест после отказа — снова без confirm", async () => {
    const { bodies } = mockStaff();
    renderApp("/staff/companies");
    const { user, dialog } = await openEditor("Меридиан Строй");

    const seats = within(dialog).getByLabelText("Рабочих мест");
    await user.clear(seats);
    await user.type(seats, "5");
    await user.click(within(dialog).getByRole("button", { name: "Сохранить" }));
    await within(dialog).findByText(/За месяц потрачено/);

    await user.clear(seats);
    await user.type(seats, "20");
    expect(within(dialog).queryByText(/За месяц потрачено/)).not.toBeInTheDocument();
    expect(within(dialog).queryByRole("checkbox")).not.toBeInTheDocument();
    await user.click(within(dialog).getByRole("button", { name: "Сохранить" }));

    expect(await screen.findByText("Сохранено: Меридиан Строй")).toBeInTheDocument();
    expect(bodies).toEqual([{ seats: 5 }, { seats: 20 }]);
  });

  it("«Без пилота» снимает срок: pilot_until: null", async () => {
    const { bodies } = mockStaff();
    renderApp("/staff/companies");
    const { user, dialog } = await openEditor("Меридиан Строй");

    const pilot = within(dialog).getByLabelText(/Пилот до/);
    expect(pilot).toHaveValue("2026-10-08");
    await user.click(within(dialog).getByRole("button", { name: "Без пилота" }));
    expect(pilot).toHaveValue("");
    expect(within(dialog).getByRole("button", { name: "Без пилота" })).toBeDisabled();
    await user.click(within(dialog).getByRole("button", { name: "Сохранить" }));

    expect(await screen.findByText("Сохранено: Меридиан Строй")).toBeInTheDocument();
    expect(bodies).toEqual([{ pilot_until: null }]);
    await waitFor(() =>
      expect(within(row("Меридиан Строй")).queryByText("осталось 4 дня")).not.toBeInTheDocument(),
    );
  });

  it("пауза — с предупреждением в окне; свою компанию не приостановить", async () => {
    const { bodies } = mockStaff();
    renderApp("/staff/companies");

    // Своя компания (та, в которой сейчас сотрудник kronto): переключатель выключен.
    const own = await openEditor("Меридиан Строй");
    const ownSwitch = within(own.dialog).getByRole("switch", { name: "Компания работает" });
    expect(ownSwitch).toBeChecked();
    expect(ownSwitch).toBeDisabled();
    expect(ownSwitch).toHaveAccessibleDescription(/из панели её не приостановить/);
    await own.user.click(within(own.dialog).getByRole("button", { name: "Отмена" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());

    const { user, dialog } = await openEditor("Восток");
    const active = within(dialog).getByRole("switch", { name: "Компания работает" });
    expect(within(dialog).queryByText("Компания встанет на паузу")).not.toBeInTheDocument();
    await user.click(active);
    expect(active).not.toBeChecked();
    expect(within(dialog).getByText("Компания встанет на паузу")).toBeInTheDocument();
    expect(dialog).toHaveTextContent("все сотрудники компании потеряют доступ");
    await user.click(within(dialog).getByRole("button", { name: "Приостановить" }));

    expect(await screen.findByText("На паузе: Восток")).toBeInTheDocument();
    expect(bodies).toEqual([{ is_active: false }]);
    expect(await within(row("Восток")).findByText("на паузе")).toBeInTheDocument();
  });

  it("включает компанию после паузы", async () => {
    const { bodies } = mockStaff();
    renderApp("/staff/companies");
    const { user, dialog } = await openEditor("Северный ветер");

    const active = within(dialog).getByRole("switch", { name: "Компания работает" });
    expect(active).not.toBeChecked();
    await user.click(active);
    await user.click(within(dialog).getByRole("button", { name: "Сохранить" }));

    expect(await screen.findByText("Снова работает: Северный ветер")).toBeInTheDocument();
    expect(bodies).toEqual([{ is_active: true }]);
  });

  it("данные удаляются только у компании на паузе и только с её кодом", async () => {
    const { deletions } = mockStaff();
    renderApp("/staff/companies");

    // Работающую — нельзя: сначала пауза.
    const working = await openEditor("Восток");
    expect(
      within(working.dialog).queryByRole("button", { name: "Удалить данные компании" }),
    ).not.toBeInTheDocument();
    await working.user.click(within(working.dialog).getByRole("button", { name: "Отмена" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());

    const { user, dialog } = await openEditor("Северный ветер");
    await user.click(within(dialog).getByRole("button", { name: "Удалить данные компании" }));
    const confirm = await screen.findByRole("dialog", { name: "Удалить данные компании?" });
    expect(confirm).toHaveTextContent("Останутся обезличенные заказы и начисления кредитов");
    const remove = within(confirm).getByRole("button", { name: "Удалить данные" });
    expect(remove).toBeDisabled();
    const code = within(confirm).getByLabelText("Код компании");
    await user.type(code, "sever");
    expect(remove).toBeDisabled();
    await user.type(code, "-9f00");
    expect(remove).toBeEnabled();
    await user.click(remove);

    expect(await screen.findByText("Данные удалены: 9f00aa11")).toBeInTheDocument();
    expect(deletions).toEqual([{ company_code: "sever-9f00" }]);
    const gone = await screen.findByRole("row", { name: /Удалённая компания 9f00aa11/ });
    expect(within(gone).getByText("данные удалены")).toBeInTheDocument();

    // У удалённой — только сведения, без правки.
    await user.click(
      within(gone).getByRole("button", { name: "Изменить: Удалённая компания 9f00aa11" }),
    );
    const info = screen.getByRole("dialog", { name: "Удалённая компания 9f00aa11" });
    expect(info).toHaveTextContent("Данные компании удалены");
    expect(within(info).queryByLabelText("Тариф")).not.toBeInTheDocument();
  });

  it("компаний нет — подсказывает, откуда они берутся", async () => {
    mockStaff([]);
    renderApp("/staff/companies");
    expect(await screen.findByText("Компаний пока нет")).toBeInTheDocument();
  });
});
