import { screen } from "@testing-library/react";
import { http, HttpResponse } from "msw";
import { describe, expect, it } from "vitest";

import type { Schemas } from "../api/client";
import { me } from "../test/fixtures";
import { renderApp } from "../test/render";
import { server } from "../test/server";

function user(n: number, active = true): Schemas["UserResponse"] {
  return {
    id: `u-${n}`,
    email: `person${n}@meridian-stroy.ru`,
    full_name: null,
    role: n === 1 ? "admin" : "employee",
    is_active: active,
    must_change_password: false,
    last_login_at: null,
    created_at: "2026-09-28T10:00:00+03:00",
  };
}

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

function admin(users: Schemas["UserResponse"][], seats: number) {
  server.use(
    http.get("/api/v1/auth/me", () => HttpResponse.json(me({ id: "u-1", role: "admin" }))),
    http.get("/api/v1/usage", () => HttpResponse.json(usage(seats))),
    http.get("/api/v1/users", () => HttpResponse.json(users)),
    http.get("/api/v1/invites", () => HttpResponse.json([])),
  );
}

describe("места компании на странице сотрудников", () => {
  it("показывает, сколько мест занято; заблокированные не считаются", async () => {
    admin([user(1), user(2), user(3, false)], 5);
    renderApp("/admin/users");

    expect(
      await screen.findByText(
        "Активных сотрудников: 2 из 5 мест. Заблокированные место не занимают.",
      ),
    ).toBeInTheDocument();
  });

  it("предупреждает, когда все места заняты", async () => {
    admin([user(1), user(2)], 2);
    renderApp("/admin/users");

    expect(await screen.findByText("Все места заняты: 2 из 2")).toBeInTheDocument();
  });
});
