import "@testing-library/jest-dom/vitest";

import { cleanup } from "@testing-library/react";
import { afterAll, afterEach, beforeAll } from "vitest";

import { setSession } from "../api/session";
import { server } from "./server";

// jsdom не умеет прокрутку.
Element.prototype.scrollIntoView = function scrollIntoView() {};

beforeAll(() => server.listen({ onUnhandledRequest: "error" }));
afterEach(() => {
  cleanup();
  setSession(null);
  server.resetHandlers();
  localStorage.clear();
  sessionStorage.clear();
});
afterAll(() => server.close());
