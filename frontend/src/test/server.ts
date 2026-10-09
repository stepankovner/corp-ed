import { http, HttpResponse } from "msw";
import { setupServer } from "msw/node";

/**
 * Обработчики по умолчанию — то, что запрашивает оболочка на любой
 * странице компании: список диалогов в боковой панели и подсказки на
 * пустом экране чата, свои подключения (предложение подключить аккаунт),
 * колокольчик, первые шаги.
 * Тест может переопределить их через server.use.
 */
export const server = setupServer(
  http.get("/api/v1/conversations", () => HttpResponse.json({ items: [], next_before: null })),
  http.get("/api/v1/suggestions", () => HttpResponse.json({ company: [], frequent: [] })),
  http.get("/api/v1/connectors/mine", () => HttpResponse.json([])),
  http.get("/api/v1/sources/mine", () => HttpResponse.json({ files: [], connectors: [] })),
  http.get("/api/v1/notifications", () => HttpResponse.json({ items: [], unread: 0 })),
  // Оплата через банк выключена (PAYMENTS_PROVIDER=none), реквизитов нет —
  // страницы тарифа и настроек компании выглядят как без неё.
  http.get("/api/v1/billing", () =>
    HttpResponse.json({
      enabled: false,
      tariff: "base",
      seats: 30,
      seat_price_kopecks: 99_000,
      quotes: [],
      grace_days: 7,
      subscription: null,
      requisites: null,
      invoices: [],
      acts: [],
    }),
  ),
  http.get("/api/v1/company/requisites", () => HttpResponse.json(null)),
  // Первые шаги пройдены — на пустом экране чата ничего лишнего.
  http.get("/api/v1/onboarding", () =>
    HttpResponse.json({
      documents: true,
      people: true,
      question: true,
      tips_seen: true,
      checklist_hidden: true,
    }),
  ),
);
