import "@testing-library/jest-dom/vitest";

import { cleanup, configure } from "@testing-library/react";
import { afterAll, afterEach, beforeAll } from "vitest";

import { setSession } from "../api/session";
import { clearPendingInvite } from "../auth/pendingInvite";
import { server } from "./server";

// findBy*/waitFor по умолчанию ждут 1 с. На загруженной машине CI (файлы
// идут параллельно, каждый поднимает свой jsdom) первый рендер страницы с
// запросами иногда не успевает — тест падает не по делу. Ждём дольше;
// на зелёном прогоне это ничего не стоит: ожидание кончается, как только
// элемент появился.
configure({ asyncUtilTimeout: 5000 });

// jsdom не умеет прокрутку.
Element.prototype.scrollIntoView = function scrollIntoView() {};
window.scrollTo = function scrollTo() {};

// Меню и подсказки Radix меряют якорь через ResizeObserver, которого в jsdom нет.
Object.defineProperty(globalThis, "ResizeObserver", {
  configurable: true,
  value: class {
    observe() {}
    unobserve() {}
    disconnect() {}
  },
});

beforeAll(() => server.listen({ onUnhandledRequest: "error" }));
afterEach(() => {
  cleanup();
  setSession(null);
  // Приглашение живёт в памяти модуля — между тестами его не переносим.
  clearPendingInvite();
  server.resetHandlers();
  localStorage.clear();
  sessionStorage.clear();
  document.documentElement.removeAttribute("data-theme");
});
afterAll(() => server.close());
