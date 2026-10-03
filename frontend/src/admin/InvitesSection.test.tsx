import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { describe, expect, it } from "vitest";

import type { Schemas } from "../api/client";
import { adminMe } from "../test/fixtures";
import { renderApp } from "../test/render";
import { server } from "../test/server";
import { inviteLink } from "./inviteLink";

type Invite = Schemas["InviteResponse"];

const TOKEN = "tok_0123456789abcdefghijklmnopqrstuvwxyz0123";
const CODE = "K7QM-4XPA";

function invite(id: string, overrides: Partial<Invite> = {}): Invite {
  return {
    id,
    created_at: "2026-10-01T12:00:00+03:00",
    expires_at: "2026-10-08T12:00:00+03:00",
    max_uses: 30,
    uses: 0,
    email_domain: null,
    requires_approval: false,
    status: "active",
    ...overrides,
  };
}

/** Администратор на странице сотрудников; приглашения — живой список. */
function admin(invites: Invite[] = []) {
  const state = { invites: [...invites] };
  server.use(
    http.get("/api/v1/auth/me", () => HttpResponse.json(adminMe())),
    http.get("/api/v1/usage", () => HttpResponse.json({ warning: false })),
    http.get("/api/v1/users", () => HttpResponse.json([])),
    http.get("/api/v1/invites", () => HttpResponse.json(state.invites)),
  );
  return state;
}

describe("приглашения в компанию", () => {
  it("ссылка без кода компании: токен после #", () => {
    expect(inviteLink("abc", "https://krontoai.ru")).toBe("https://krontoai.ru/join#abc");
  });

  it("с одобрением: ссылка /join#токен и код показываются один раз", async () => {
    const user = userEvent.setup();
    const state = admin();
    let createBody: unknown;
    server.use(
      http.post("/api/v1/invites", async ({ request }) => {
        createBody = await request.json();
        const created = invite("i-1", {
          email_domain: "meridian-stroy.ru",
          requires_approval: true,
        });
        state.invites = [created];
        return HttpResponse.json({ invite: created, token: TOKEN, code: CODE }, { status: 201 });
      }),
    );
    renderApp("/admin/users");

    await user.click(await screen.findByRole("button", { name: "Пригласить" }));
    const approval = screen.getByRole("checkbox", { name: "Требовать одобрения администратора" });
    expect(approval).not.toBeChecked();
    expect(approval).toHaveAccessibleDescription(/одобрит заявку/);
    await user.click(approval);
    await user.type(screen.getByLabelText(/Только почта домена/), "meridian-stroy.ru");
    await user.click(screen.getByRole("button", { name: "Создать приглашение" }));

    const dialog = await screen.findByRole("dialog", { name: "Приглашение" });
    expect(createBody).toEqual({
      ttl_days: 7,
      max_uses: null,
      email_domain: "meridian-stroy.ru",
      requires_approval: true,
    });
    expect(within(dialog).getByTestId("invite-link").textContent).toBe(
      `${window.location.origin}/join#${TOKEN}`,
    );
    expect(within(dialog).getByTestId("invite-code").textContent).toBe(CODE);
    expect(dialog).toHaveTextContent("«Вступить по коду»");
    expect(dialog).toHaveTextContent("«Ждут одобрения»");

    await user.click(within(dialog).getByRole("button", { name: "Скопировать код" }));
    expect(await navigator.clipboard.readText()).toBe(CODE);

    await user.click(within(dialog).getByRole("button", { name: "Готово" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    // В списке ни ссылки, ни кода — только состояние приглашения.
    expect(screen.queryByText(CODE)).not.toBeInTheDocument();
    const table = await screen.findByRole("table", { name: "Приглашения" });
    expect(within(table).getByText("с одобрением")).toBeInTheDocument();
    expect(within(table).getByText("@meridian-stroy.ru")).toBeInTheDocument();
  });

  it("одобрение по умолчанию выключено", async () => {
    const user = userEvent.setup();
    admin();
    let createBody: unknown;
    server.use(
      http.post("/api/v1/invites", async ({ request }) => {
        createBody = await request.json();
        return HttpResponse.json(
          { invite: invite("i-1"), token: TOKEN, code: CODE },
          { status: 201 },
        );
      }),
    );
    renderApp("/admin/users");

    await user.click(await screen.findByRole("button", { name: "Пригласить" }));
    await user.click(screen.getByRole("button", { name: "Создать приглашение" }));

    const dialog = await screen.findByRole("dialog", { name: "Приглашение" });
    expect(createBody).toMatchObject({ requires_approval: false, email_domain: null });
    expect(dialog).not.toHaveTextContent("Ждут одобрения");
  });

  it("список с состоянием и одобрением; отзыв — после подтверждения", async () => {
    const user = userEvent.setup();
    const state = admin([
      invite("i-1", { requires_approval: true, uses: 3 }),
      invite("i-2", { status: "expired" }),
    ]);
    let revoked: unknown = null;
    server.use(
      http.delete("/api/v1/invites/:id", ({ params }) => {
        revoked = params.id;
        state.invites = state.invites.map((i) =>
          i.id === params.id ? { ...i, status: "revoked" } : i,
        );
        return new HttpResponse(null, { status: 204 });
      }),
    );
    renderApp("/admin/users");

    const table = await screen.findByRole("table", { name: "Приглашения" });
    const [, first, second] = within(table).getAllByRole("row");
    if (!first || !second) throw new Error("нет строк приглашений");
    expect(first).toHaveTextContent("3 из 30");
    expect(within(first).getByText("с одобрением")).toBeInTheDocument();
    expect(within(first).getByText("действует")).toBeInTheDocument();
    expect(within(second).getByText("сразу")).toBeInTheDocument();
    expect(within(second).getByText("истекло")).toBeInTheDocument();
    // Истёкшее отзывать незачем.
    expect(within(second).queryByRole("button")).not.toBeInTheDocument();

    await user.click(within(first).getByRole("button", { name: "Отозвать" }));
    const dialog = await screen.findByRole("dialog", { name: "Отозвать приглашение?" });
    expect(revoked).toBeNull();
    await user.click(within(dialog).getByRole("button", { name: "Отозвать" }));

    await waitFor(() => expect(within(first).getByText("отозвано")).toBeInTheDocument());
    expect(revoked).toBe("i-1");
    expect(within(first).queryByRole("button", { name: "Отозвать" })).not.toBeInTheDocument();
  });
});
