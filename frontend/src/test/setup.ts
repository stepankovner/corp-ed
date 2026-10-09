import "@testing-library/jest-dom/vitest";

import { cleanup } from "@testing-library/react";
import { afterAll, afterEach, beforeAll } from "vitest";

import { setSession } from "../api/session";
import { clearPendingInvite } from "../auth/pendingInvite";
import { server } from "./server";

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
