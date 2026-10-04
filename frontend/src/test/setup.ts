import "@testing-library/jest-dom/vitest";

import { cleanup } from "@testing-library/react";
import { afterAll, afterEach, beforeAll } from "vitest";

import { setSession } from "../api/session";
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
  server.resetHandlers();
  localStorage.clear();
  sessionStorage.clear();
  document.documentElement.removeAttribute("data-theme");
});
afterAll(() => server.close());
