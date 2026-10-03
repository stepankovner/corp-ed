import { http, HttpResponse } from "msw";
import { setupServer } from "msw/node";

/**
 * Обработчики по умолчанию — то, что запрашивает оболочка на любой
 * странице компании: список диалогов в боковой панели и подсказки на
 * пустом экране чата. Тест может переопределить их через server.use.
 */
export const server = setupServer(
  http.get("/api/v1/conversations", () => HttpResponse.json({ items: [], next_before: null })),
  http.get("/api/v1/suggestions", () => HttpResponse.json({ company: [], frequent: [] })),
);
