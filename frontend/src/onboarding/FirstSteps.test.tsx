import { act, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { describe, expect, it } from "vitest";

import type { Schemas } from "../api/client";
import { adminMe, me } from "../test/fixtures";
import { renderApp } from "../test/render";
import { server } from "../test/server";

type Onboarding = Schemas["OnboardingResponse"];
type Me = Schemas["MeResponse"];

function state(overrides: Partial<Onboarding> = {}): Onboarding {
  return {
    documents: false,
    people: false,
    question: false,
    tips_seen: false,
    checklist_hidden: false,
    ...overrides,
  };
}

/** Вход и первые шаги; вернёт, сколько раз их запросили. */
function setup(user: Me, onboarding: Onboarding | (() => Response)) {
  const calls = { get: 0 };
  server.use(
    http.get("/api/v1/auth/me", () => HttpResponse.json(user)),
    // Оболочка: колокольчик и плашка лимита администратора.
    http.get("/api/v1/notifications", () => HttpResponse.json({ items: [], unread: 0 })),
    http.get("/api/v1/usage", () => HttpResponse.json({ warning: false })),
    http.get("/api/v1/onboarding", () => {
      calls.get += 1;
      return typeof onboarding === "function" ? onboarding() : HttpResponse.json(onboarding);
    }),
  );
  return calls;
}

/** Ответ на первые шаги пришёл и отрисован (или не отрисован). */
async function settled(calls: { get: number }) {
  await screen.findByText(/Я отвечаю по документам/);
  await waitFor(() => expect(calls.get).toBeGreaterThan(0));
  await act(() => new Promise((resolve) => setTimeout(resolve, 30)));
}

describe("первые шаги администратора", () => {
  it("показывает шаги, прогресс и ссылки на разделы", async () => {
    setup(adminMe(), state({ documents: true }));
    renderApp("/");

    const box = await screen.findByRole("region", { name: "Первые шаги" });
    expect(within(box).getByText("1 из 3")).toBeInTheDocument();
    const steps = within(box).getAllByRole("listitem");
    expect(steps).toHaveLength(3);

    // Сделанный шаг — без ссылки, с отметкой для скринридера.
    expect(steps[0]).toHaveTextContent("Загрузите документы или подключите источник — готово");
    expect(within(steps[0]!).queryByRole("link")).not.toBeInTheDocument();
    expect(within(steps[1]!).getByRole("link", { name: "Пригласите сотрудников" })).toHaveAttribute(
      "href",
      "/admin/users",
    );
    expect(steps[1]).toHaveTextContent("— не сделано");
    expect(steps[2]).toHaveTextContent("Задайте первый вопрос — прямо здесь, в поле ниже");
    expect(within(steps[2]!).queryByRole("link")).not.toBeInTheDocument();
  });

  it("ничего не сделано — ссылка на загрузку документов, 0 из 3", async () => {
    setup(adminMe(), state());
    renderApp("/");
    const box = await screen.findByRole("region", { name: "Первые шаги" });
    expect(within(box).getByText("0 из 3")).toBeInTheDocument();
    expect(
      within(box).getByRole("link", { name: "Загрузите документы или подключите источник" }),
    ).toHaveAttribute("href", "/admin/sources/files");
    // Подсказки сотруднику администратору не показываем.
    expect(screen.queryByRole("region", { name: "Несколько советов" })).not.toBeInTheDocument();
  });

  it("«Скрыть» прячет чек-лист и запоминает это на сервере", async () => {
    setup(adminMe(), state({ people: true }));
    let hidden = 0;
    server.use(
      http.post("/api/v1/onboarding/checklist", () => {
        hidden += 1;
        return HttpResponse.json(state({ people: true, checklist_hidden: true }));
      }),
    );
    renderApp("/");
    const user = userEvent.setup();
    const box = await screen.findByRole("region", { name: "Первые шаги" });
    expect(within(box).getByText("1 из 3")).toBeInTheDocument();

    await user.click(within(box).getByRole("button", { name: "Скрыть первые шаги" }));
    await waitFor(() =>
      expect(screen.queryByRole("region", { name: "Первые шаги" })).not.toBeInTheDocument(),
    );
    await waitFor(() => expect(hidden).toBe(1));
    expect(screen.getByLabelText("Ваш вопрос")).toHaveFocus();
  });

  it("все три шага сделаны — чек-листа нет", async () => {
    const calls = setup(adminMe(), state({ documents: true, people: true, question: true }));
    renderApp("/");
    await settled(calls);
    expect(screen.queryByRole("region", { name: "Первые шаги" })).not.toBeInTheDocument();
  });

  it("скрыт раньше — чек-листа нет", async () => {
    const calls = setup(adminMe(), state({ checklist_hidden: true }));
    renderApp("/");
    await settled(calls);
    expect(screen.queryByRole("region", { name: "Первые шаги" })).not.toBeInTheDocument();
  });

  it("сервер не ответил — пустой экран чата как обычно", async () => {
    const calls = setup(adminMe(), () =>
      HttpResponse.json({ detail: "Что-то сломалось" }, { status: 500 }),
    );
    renderApp("/");
    await settled(calls);
    expect(screen.queryByRole("region", { name: "Первые шаги" })).not.toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Здравствуйте, Анна!" })).toBeInTheDocument();
    expect(screen.getByLabelText("Ваш вопрос")).toBeInTheDocument();
  });
});

describe("подсказки сотруднику", () => {
  it("показывает советы и прячет их по «Понятно», не дожидаясь сервера", async () => {
    setup(me(), state());
    let release = () => {};
    const gate = new Promise<void>((resolve) => {
      release = resolve;
    });
    let seen = 0;
    server.use(
      http.post("/api/v1/onboarding/tips", async () => {
        seen += 1;
        await gate;
        return HttpResponse.json(state({ tips_seen: true }));
      }),
    );
    renderApp("/");
    const user = userEvent.setup();

    const box = await screen.findByRole("region", { name: "Несколько советов" });
    const tips = within(box).getAllByRole("listitem");
    expect(tips.map((tip) => tip.textContent)).toEqual([
      "Спрашивайте как коллегу — обычными словами",
      "В каждом ответе — ссылка на документ: нажмите, чтобы увидеть фрагмент",
      "Приложите файл скрепкой — спросите по договору или приказу",
      "👍 и 👎 под ответом помогают администратору улучшать документы",
    ]);
    expect(within(box).getByRole("link", { name: "Подробнее — в «Помощи»" })).toHaveAttribute(
      "href",
      "/help",
    );
    // Чек-лист — только администратору.
    expect(screen.queryByRole("region", { name: "Первые шаги" })).not.toBeInTheDocument();

    await user.click(within(box).getByRole("button", { name: "Понятно" }));
    await waitFor(() =>
      expect(screen.queryByRole("region", { name: "Несколько советов" })).not.toBeInTheDocument(),
    );
    await waitFor(() => expect(seen).toBe(1));
    expect(screen.getByLabelText("Ваш вопрос")).toHaveFocus();
    release();
  });

  it("не сохранилось — советы возвращаются с сообщением об ошибке", async () => {
    setup(me(), state());
    server.use(
      http.post("/api/v1/onboarding/tips", () =>
        HttpResponse.json({ detail: "Сервис временно недоступен" }, { status: 503 }),
      ),
    );
    renderApp("/");
    const user = userEvent.setup();
    const box = await screen.findByRole("region", { name: "Несколько советов" });
    await user.click(within(box).getByRole("button", { name: "Понятно" }));

    expect(await screen.findByText("Сервис временно недоступен")).toBeInTheDocument();
    expect(await screen.findByRole("region", { name: "Несколько советов" })).toBeInTheDocument();
  });

  it("советы уже видел — их нет", async () => {
    const calls = setup(me(), state({ tips_seen: true }));
    renderApp("/");
    await settled(calls);
    expect(screen.queryByRole("region", { name: "Несколько советов" })).not.toBeInTheDocument();
  });
});
