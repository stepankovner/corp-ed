import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { me } from "../test/fixtures";
import { renderApp } from "../test/render";
import { server } from "../test/server";

const BITRIX = {
  id: "c-2",
  kind: "bitrix24",
  name: "Битрикс24",
  oauth: true,
  grant_status: null,
  grant_error_code: null,
};

function signedIn() {
  server.use(http.get("/api/v1/auth/me", () => HttpResponse.json(me())));
}

beforeEach(() => localStorage.clear());

describe("где ищет ассистент", () => {
  it("показывает папки сотрудника и источники компании с его состоянием", async () => {
    signedIn();
    server.use(
      http.get("/api/v1/connectors/mine", () => HttpResponse.json([BITRIX])),
      http.get("/api/v1/sources/mine", () =>
        HttpResponse.json({
          files: [
            { folder_id: null, name: "Общие документы", restricted: false, documents: 12 },
            { folder_id: "f-1", name: "Бухгалтерия", restricted: true, documents: 3 },
          ],
          connectors: [
            {
              id: "c-1",
              kind: "confluence",
              name: "Confluence",
              mode: "organization",
              working: true,
              grant_status: null,
            },
            {
              id: "c-2",
              kind: "bitrix24",
              name: "Битрикс24",
              mode: "per_user",
              working: true,
              grant_status: null,
            },
          ],
        }),
      ),
    );
    renderApp("/settings/connections");

    const section = await screen.findByRole("region", { name: "Где ищет ассистент" });
    expect(await within(section).findByText("Общие документы")).toBeInTheDocument();
    expect(within(section).getByText("12 документов")).toBeInTheDocument();
    expect(within(section).getByText("Бухгалтерия")).toBeInTheDocument();
    expect(within(section).getByText("для вашего отдела")).toBeInTheDocument();
    expect(within(section).getByText("3 документа")).toBeInTheDocument();
    expect(within(section).getByText("подключено компанией")).toBeInTheDocument();
    expect(within(section).getByText("ищет")).toBeInTheDocument();
    expect(within(section).getByText("ваш личный аккаунт")).toBeInTheDocument();
    expect(within(section).getByText("не подключён")).toBeInTheDocument();

    expect(screen.getByRole("heading", { name: "Ваши аккаунты" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Подключить" })).toBeInTheDocument();
  });

  it("пусто — объясняет, что ассистенту пока не из чего отвечать", async () => {
    signedIn();
    renderApp("/settings/connections");
    const section = await screen.findByRole("region", { name: "Где ищет ассистент" });
    expect(
      await within(section).findByText(/Пока ассистенту не из чего отвечать/),
    ).toBeInTheDocument();
  });
});

describe("предложение подключить свой аккаунт", () => {
  it("на пустом экране чата ведёт в настройки и скрывается насовсем", async () => {
    signedIn();
    const yandex = { ...BITRIX, id: "c-3", kind: "yandex360", name: "Яндекс 360" };
    server.use(http.get("/api/v1/connectors/mine", () => HttpResponse.json([BITRIX, yandex])));
    const user = userEvent.setup();
    const { unmount } = renderApp("/");

    const note = await screen.findByRole("note");
    expect(note).toHaveTextContent(
      "Подключите свой Битрикс24, чтобы ассистент искал и по вашим файлам.",
    );
    expect(within(note).getByRole("link", { name: "Подключить" })).toHaveAttribute(
      "href",
      "/settings/connections",
    );
    await user.click(within(note).getByRole("button", { name: "Скрыть" }));
    expect(await screen.findByRole("note")).toHaveTextContent("Подключите свой Яндекс 360");

    // Скрытое помнится после перезагрузки: предлагаем следующий источник.
    unmount();
    renderApp("/");
    expect(await screen.findByRole("note")).toHaveTextContent("Подключите свой Яндекс 360");
  });

  it("не показывает, когда аккаунт подключён", async () => {
    signedIn();
    let asked = false;
    server.use(
      http.get("/api/v1/connectors/mine", () => {
        asked = true;
        return HttpResponse.json([{ ...BITRIX, grant_status: "active" }]);
      }),
    );
    renderApp("/");
    await screen.findByRole("heading", { name: /Здравствуйте/ });
    await vi.waitFor(() => expect(asked).toBe(true));
    expect(screen.queryByRole("note")).not.toBeInTheDocument();
  });
});
