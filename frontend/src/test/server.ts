import { http, HttpResponse } from "msw";
import { setupServer } from "msw/node";

/**
 * Обработчики по умолчанию — то, что запрашивает оболочка на любой
 * странице компании: список диалогов в боковой панели и подсказки на
 * пустом экране чата, свои подключения (предложение подключить аккаунт),
 * колокольчик.
 * Тест может переопределить их через server.use.
 */
export const server = setupServer(
  http.get("/api/v1/conversations", () => HttpResponse.json({ items: [], next_before: null })),
  http.get("/api/v1/suggestions", () => HttpResponse.json({ company: [], frequent: [] })),
  http.get("/api/v1/connectors/mine", () => HttpResponse.json([])),
  http.get("/api/v1/sources/mine", () => HttpResponse.json({ files: [], connectors: [] })),
  http.get("/api/v1/notifications", () => HttpResponse.json({ items: [], unread: 0 })),
);
