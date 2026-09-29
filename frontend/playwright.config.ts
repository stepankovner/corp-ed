import { defineConfig, devices } from "@playwright/test";

/**
 * Сквозные проверки MVP против настоящего бэкенда (LLM_PROVIDER=fake).
 * Стенд поднимает CI (.github/workflows/ci.yaml, задание e2e) или
 * разработчик: API + воркер + `npm run dev`; компания — свежая, из
 * `cli create-tenant`, её код и временный пароль — в переменных E2E_*.
 */
const executablePath = process.env.PW_CHROMIUM_PATH;

export default defineConfig({
  testDir: "./e2e",
  timeout: 90_000,
  expect: { timeout: 15_000 },
  fullyParallel: false,
  workers: 1,
  retries: 0,
  forbidOnly: Boolean(process.env.CI),
  reporter: process.env.CI ? [["list"], ["html", { open: "never" }]] : "list",
  use: {
    baseURL: process.env.E2E_BASE_URL ?? "http://localhost:5173",
    locale: "ru-RU",
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
  },
  projects: [
    {
      name: "chromium",
      use: {
        ...devices["Desktop Chrome"],
        ...(executablePath ? { launchOptions: { executablePath } } : {}),
      },
    },
  ],
});
