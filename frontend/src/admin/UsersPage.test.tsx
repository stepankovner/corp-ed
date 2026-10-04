import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { describe, expect, it } from "vitest";

import type { Schemas } from "../api/client";
import { adminMe } from "../test/fixtures";
import { renderApp } from "../test/render";
import { server } from "../test/server";

type Member = Schemas["UserResponse"];

function member(id: string, overrides: Partial<Member> = {}): Member {
  return {
    id,
    email: `${id}@meridian-stroy.ru`,
    full_name: null,
    role: "employee",
    status: "active",
    position: null,
    department_id: null,
    last_login_at: null,
    created_at: "2026-10-01T10:00:00+03:00",
    ...overrides,
  };
}

// Сам администратор: id членства совпадает с company.member_id из adminMe().
const SELF = member("m-1", {
  email: "anna@meridian-stroy.ru",
  full_name: "Анна Смирнова",
  role: "admin",
});
const PETR = member("m-2", { email: "petr@meridian-stroy.ru", full_name: "Пётр Орлов" });

function usage(seats: number): Schemas["UsageResponse"] {
  return {
    period_start: "2026-09-01T00:00:00+03:00",
    period_end: "2026-10-01T00:00:00+03:00",
    seats,
    credits_per_seat: 420,
    pool: seats * 420,
    used: 0,
    remaining: seats * 420,
    exhausted: false,
    warn_at_percent: 80,
    warning: false,
  };
}

/** Администратор и люди компании; список живой — ручки ниже его меняют. */
function company(people: Member[], seats = 30) {
  const state = { people: [...people] };
  server.use(
    http.get("/api/v1/auth/me", () => HttpResponse.json(adminMe())),
    http.get("/api/v1/usage", () => HttpResponse.json(usage(seats))),
    http.get("/api/v1/users", () => HttpResponse.json(state.people)),
    http.get("/api/v1/invites", () => HttpResponse.json([])),
    http.get("/api/v1/departments", () =>
      HttpResponse.json([{ id: "d-1", name: "Продажи", members: 1 }]),
    ),
  );
  return state;
}

function row(name: string): HTMLElement {
  const cell = screen.getByText(name, { selector: "td *" });
  const tr = cell.closest("tr");
  if (!tr) throw new Error(`нет строки ${name}`);
  return tr;
}

async function openActions(user: ReturnType<typeof userEvent.setup>, name: string) {
  await user.click(screen.getByRole("button", { name: `Действия: ${name}` }));
  return screen.findByRole("menu");
}

describe("места компании на странице сотрудников", () => {
  it("считает только работающих: заблокированные и ждущие одобрения место не занимают", async () => {
    company(
      [
        SELF,
        PETR,
        member("m-3", { status: "blocked" }),
        member("m-4", { status: "pending", full_name: "Ольга Ким" }),
      ],
      5,
    );
    renderApp("/admin/users");

    expect(
      await screen.findByText(
        "Занято мест: 2 из 5. Заблокированные и ждущие одобрения место не занимают.",
      ),
    ).toBeInTheDocument();
  });

  it("предупреждает, когда все места заняты", async () => {
    company([SELF, PETR], 2);
    renderApp("/admin/users");

    expect(await screen.findByText("Все места заняты: 2 из 2")).toBeInTheDocument();
  });
});

describe("люди компании", () => {
  it("себе действий нет; учёток и временных паролей админ больше не заводит", async () => {
    company([SELF, PETR, member("m-3", { status: "blocked" })]);
    renderApp("/admin/users");

    expect(await screen.findByRole("button", { name: "Действия: Пётр Орлов" })).toBeInTheDocument();
    const self = row("Анна Смирнова");
    expect(within(self).getByText("вы")).toBeInTheDocument();
    expect(within(self).queryByRole("button")).not.toBeInTheDocument();
    expect(within(row("m-3@meridian-stroy.ru")).getByText("заблокирован")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Пригласить" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Добавить сотрудника/ })).not.toBeInTheDocument();
    expect(screen.queryByText(/временн\w* парол/i)).not.toBeInTheDocument();
  });

  it("блокировка и смена роли — из меню строки", async () => {
    const user = userEvent.setup();
    const state = company([SELF, PETR]);
    const bodies: unknown[] = [];
    server.use(
      http.patch("/api/v1/users/:id", async ({ params, request }) => {
        const change = (await request.json()) as Schemas["UserUpdateRequest"];
        bodies.push({ id: params.id, ...change });
        state.people = state.people.map((p) =>
          p.id === params.id
            ? {
                ...p,
                ...(change.role ? { role: change.role } : {}),
                ...(change.blocked !== undefined && change.blocked !== null
                  ? { status: change.blocked ? "blocked" : "active" }
                  : {}),
              }
            : p,
        );
        return HttpResponse.json(state.people.find((p) => p.id === params.id));
      }),
    );
    renderApp("/admin/users");
    await screen.findByText("Пётр Орлов");

    let menu = await openActions(user, "Пётр Орлов");
    await user.click(within(menu).getByRole("menuitem", { name: "Заблокировать" }));
    expect(await screen.findByText("Доступ закрыт: Пётр Орлов")).toBeInTheDocument();
    await waitFor(() => expect(within(row("Пётр Орлов")).getByText("заблокирован")).toBeVisible());

    menu = await openActions(user, "Пётр Орлов");
    expect(within(menu).getByRole("menuitem", { name: "Разблокировать" })).toBeInTheDocument();
    await user.click(within(menu).getByRole("menuitem", { name: "Сделать администратором" }));
    expect(await screen.findByText("Пётр Орлов — теперь администратор")).toBeInTheDocument();
    await waitFor(() => expect(within(row("Пётр Орлов")).getByText("администратор")).toBeVisible());

    expect(bodies).toEqual([
      { id: "m-2", blocked: true },
      { id: "m-2", role: "admin" },
    ]);
  });

  it("должность и отдел: видны под именем, правятся из меню строки", async () => {
    const user = userEvent.setup();
    const state = company([SELF, { ...PETR, position: "Инженер", department_id: "d-1" }]);
    let body: unknown;
    server.use(
      http.patch("/api/v1/people/:memberId", async ({ request, params }) => {
        body = { memberId: params.memberId, ...((await request.json()) as object) };
        state.people = state.people.map((m) =>
          m.id === "m-2" ? { ...m, position: "Ведущий инженер" } : m,
        );
        return HttpResponse.json({});
      }),
    );
    renderApp("/admin/users");

    expect(await screen.findByText("Инженер · Продажи")).toBeInTheDocument();
    const menu = await openActions(user, "Пётр Орлов");
    await user.click(within(menu).getByRole("menuitem", { name: "Должность и отдел" }));
    const dialog = screen.getByRole("dialog", { name: "Должность и отдел" });
    const position = within(dialog).getByLabelText(/^Должность/);
    expect(position).toHaveValue("Инженер");
    await user.clear(position);
    await user.type(position, "Ведущий инженер");
    await user.click(within(dialog).getByRole("button", { name: "Сохранить" }));

    expect(await screen.findByText("Ведущий инженер · Продажи")).toBeInTheDocument();
    expect(body).toEqual({ memberId: "m-2", position: "Ведущий инженер", department_id: "d-1" });
  });

  it("отказ сервера (последний администратор) — во всплывающем сообщении", async () => {
    const user = userEvent.setup();
    company([SELF, member("m-2", { full_name: "Пётр Орлов", role: "admin" })]);
    server.use(
      http.patch("/api/v1/users/:id", () =>
        HttpResponse.json(
          { detail: "В компании должен остаться хотя бы один администратор", code: "last_admin" },
          { status: 409 },
        ),
      ),
    );
    renderApp("/admin/users");
    await screen.findByText("Пётр Орлов");

    const menu = await openActions(user, "Пётр Орлов");
    await user.click(within(menu).getByRole("menuitem", { name: "Сделать сотрудником" }));

    expect(
      await screen.findByText("В компании должен остаться хотя бы один администратор"),
    ).toBeInTheDocument();
  });

  it("убрать из компании — после подтверждения; учётка остаётся, место освобождается", async () => {
    const user = userEvent.setup();
    const state = company([SELF, PETR]);
    let removed: unknown = null;
    server.use(
      http.delete("/api/v1/users/:id", ({ params }) => {
        removed = params.id;
        state.people = state.people.filter((p) => p.id !== params.id);
        return new HttpResponse(null, { status: 204 });
      }),
    );
    renderApp("/admin/users");
    await screen.findByText("Пётр Орлов");

    const menu = await openActions(user, "Пётр Орлов");
    await user.click(within(menu).getByRole("menuitem", { name: "Убрать из компании" }));
    const dialog = await screen.findByRole("dialog", { name: "Убрать из компании?" });
    expect(dialog).toHaveTextContent("Учётка kronto останется");
    expect(dialog).toHaveTextContent("Рабочее место освободится");
    expect(removed).toBeNull();

    await user.click(within(dialog).getByRole("button", { name: "Убрать" }));

    expect(await screen.findByText("Пётр Орлов больше не в компании")).toBeInTheDocument();
    expect(removed).toBe("m-2");
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    await waitFor(() =>
      expect(
        screen.queryByRole("button", { name: "Действия: Пётр Орлов" }),
      ).not.toBeInTheDocument(),
    );
  });
});

describe("заявки на вступление", () => {
  it("одобрить и отклонить", async () => {
    const user = userEvent.setup();
    const olga = member("m-4", { full_name: "Ольга Ким", status: "pending" });
    const ivan = member("m-5", { full_name: "Иван Белов", status: "pending" });
    const state = company([SELF, olga, ivan]);
    const calls: string[] = [];
    server.use(
      http.post("/api/v1/users/:id/approve", ({ params }) => {
        calls.push(`approve ${String(params.id)}`);
        state.people = state.people.map((p) =>
          p.id === params.id ? { ...p, status: "active" } : p,
        );
        return HttpResponse.json(state.people.find((p) => p.id === params.id));
      }),
      http.post("/api/v1/users/:id/reject", ({ params }) => {
        calls.push(`reject ${String(params.id)}`);
        state.people = state.people.filter((p) => p.id !== params.id);
        return new HttpResponse(null, { status: 204 });
      }),
    );
    renderApp("/admin/users");

    expect(await screen.findByRole("heading", { name: "Ждут одобрения" })).toBeInTheDocument();
    const requests = screen.getByRole("list", { name: "Заявки на вступление" });
    const olgaCard = within(requests).getByText("Ольга Ким").closest("li");
    const ivanCard = within(requests).getByText("Иван Белов").closest("li");
    if (!olgaCard || !ivanCard) throw new Error("нет карточек заявок");
    // Ждущих одобрения нет в таблице работающих.
    expect(screen.queryByRole("button", { name: "Действия: Ольга Ким" })).not.toBeInTheDocument();

    await user.click(within(olgaCard).getByRole("button", { name: "Одобрить" }));
    expect(await screen.findByText("Заявка одобрена: Ольга Ким")).toBeInTheDocument();
    expect(await screen.findByRole("button", { name: "Действия: Ольга Ким" })).toBeInTheDocument();

    await user.click(within(ivanCard).getByRole("button", { name: "Отклонить" }));
    expect(await screen.findByText("Заявка отклонена: Иван Белов")).toBeInTheDocument();
    await waitFor(() =>
      expect(screen.queryByRole("heading", { name: "Ждут одобрения" })).not.toBeInTheDocument(),
    );
    expect(calls).toEqual(["approve m-4", "reject m-5"]);
  });

  it("без свободных мест одобрить нельзя — ответ сервера виден", async () => {
    const user = userEvent.setup();
    company([SELF, member("m-4", { full_name: "Ольга Ким", status: "pending" })], 1);
    server.use(
      http.post("/api/v1/users/:id/approve", () =>
        HttpResponse.json(
          { detail: "Все места заняты: активных сотрудников 1 из 1." },
          { status: 409 },
        ),
      ),
    );
    renderApp("/admin/users");

    const requests = await screen.findByRole("list", { name: "Заявки на вступление" });
    await user.click(within(requests).getByRole("button", { name: "Одобрить" }));

    expect(
      await screen.findByText("Все места заняты: активных сотрудников 1 из 1."),
    ).toBeInTheDocument();
  });
});
