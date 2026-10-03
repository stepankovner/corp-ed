import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { describe, expect, it } from "vitest";

import type { Schemas } from "../api/client";
import { adminMe } from "../test/fixtures";
import { renderApp } from "../test/render";
import { server } from "../test/server";

type Analytics = Schemas["AnalyticsResponse"];

function analytics(overrides: Partial<Analytics> = {}): Analytics {
  return {
    since: "2026-09-04",
    until: "2026-10-03",
    questions: 40,
    answered: 30,
    general: 6,
    refused: 4,
    likes: 9,
    dislikes: 1,
    reasons: { outdated: 2, inaccurate: 1 },
    active_people: 12,
    members: 20,
    credits: 52,
    days: [
      { day: "2026-10-02", questions: 15, answered: 10 },
      { day: "2026-10-03", questions: 25, answered: 20 },
    ],
    frequent: [{ question: "Какие суточные?", asked: 7, people: 4, answered: 0 }],
    comments: [
      {
        created_at: "2026-10-03T10:00:00Z",
        question: "Сколько дней отпуска?",
        reason: "outdated",
        comment: "Приказ уже поменяли",
      },
    ],
    open_gaps: 2,
    ...overrides,
  };
}

function mockAnalytics(make: (days: number) => Analytics) {
  const asked: number[] = [];
  server.use(
    http.get("/api/v1/auth/me", () => HttpResponse.json(adminMe())),
    http.get("/api/v1/usage", () => HttpResponse.json({ warning: false })),
    http.get("/api/v1/analytics", ({ request }) => {
      const days = Number(new URL(request.url).searchParams.get("days"));
      asked.push(days);
      return HttpResponse.json(make(days));
    }),
  );
  return asked;
}

describe("обзор админки", () => {
  it("показывает обезличенную статистику и меняет период", async () => {
    const asked = mockAnalytics((days) =>
      days === 7
        ? analytics({ questions: 5, answered: 5, general: 0, refused: 0, likes: 0, dislikes: 0 })
        : analytics(),
    );
    const user = userEvent.setup();
    const { router } = renderApp("/admin");

    expect(await screen.findByRole("heading", { name: "Обзор" })).toBeInTheDocument();
    await waitFor(() => expect(router.state.location.pathname).toBe("/admin/overview"));
    expect(document.title).toBe("Обзор — kronto");
    expect(await screen.findByText("75 %")).toBeInTheDocument();
    expect(screen.getByText("90 %")).toBeInTheDocument();
    expect(screen.getByText("без ответа в документах")).toBeInTheDocument();
    expect(screen.getByText("общий ответ — 6, отказ — 4")).toBeInTheDocument();
    expect(screen.getByText("из 20 в компании")).toBeInTheDocument();
    expect(screen.getByText("👍 9 · 👎 1")).toBeInTheDocument();

    const frequent = screen.getByRole("region", { name: "Частые вопросы" });
    expect(within(frequent).getByText("Какие суточные?")).toBeInTheDocument();
    expect(within(frequent).getByText("ответа нет в документах")).toBeInTheDocument();

    const feedback = screen.getByRole("region", { name: "Что не понравилось" });
    expect(within(feedback).getByText(/^Устаревшие сведения ·/)).toBeInTheDocument();
    expect(within(feedback).getByText("«Приказ уже поменяли»")).toBeInTheDocument();
    expect(within(feedback).getByText(/На вопрос «Сколько дней отпуска\?»/)).toBeInTheDocument();

    const table = screen.getByRole("table", { name: "Вопросы по дням" });
    expect(within(table).getAllByRole("row")).toHaveLength(3);
    expect(screen.getByRole("link", { name: /2 пробела в документах/ })).toHaveAttribute(
      "href",
      "/admin/gaps",
    );

    await user.click(screen.getByRole("button", { name: "7 дней" }));
    expect(await screen.findByText("100 %")).toBeInTheDocument();
    expect(asked).toEqual([30, 7]);
  });

  it("без вопросов — пустое состояние, частые вопросы не выдумывает", async () => {
    mockAnalytics(() =>
      analytics({
        questions: 0,
        answered: 0,
        general: 0,
        refused: 0,
        likes: 0,
        dislikes: 0,
        reasons: {},
        active_people: 0,
        days: [],
        frequent: [],
        comments: [],
        open_gaps: 0,
      }),
    );
    renderApp("/admin/overview");
    expect(await screen.findByText("Вопросов пока не было")).toBeInTheDocument();
    expect(screen.queryByRole("region", { name: "Частые вопросы" })).not.toBeInTheDocument();
    expect(screen.queryByRole("link", { name: /пробел/ })).not.toBeInTheDocument();
    expect(screen.getByText("оценок пока нет")).toBeInTheDocument();
  });
});
