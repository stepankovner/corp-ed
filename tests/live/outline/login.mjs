// Вход в Outline стенда через dex (OIDC) в браузере: первый вошедший
// становится администратором рабочей области, остальные — участниками.
// С флагом --api-key печатает API-ключ, созданный из сессии (как
// «Настройки → API» в интерфейсе).
// Запуск из frontend/:
//   node ../tests/live/outline/login.mjs <адрес Outline> <почта> <пароль> [--api-key]
// CHROMIUM_PATH — как у ../confluence_dc/wizard.mjs. Сертификат стенда
// самоподписанный — браузер стенда его не проверяет (только 127.0.0.1).
import { createRequire } from "node:module";

const { chromium } = createRequire(`${process.cwd()}/package.json`)("playwright");
const [base, email, password, flag] = process.argv.slice(2);
if (!base || !email || !password) throw new Error("нужны адрес, почта и пароль");
const origin = base.replace(/\/$/, "");

const browser = await chromium.launch(
  process.env.CHROMIUM_PATH ? { executablePath: process.env.CHROMIUM_PATH } : {},
);
const context = await browser.newContext({ ignoreHTTPSErrors: true });
const page = await context.newPage();
page.setDefaultTimeout(60_000);

await page.goto(`${origin}/`);
// С единственным провайдером Outline сразу уводит в dex; иначе — кнопка.
const login = page.locator("#login");
const button = page.getByRole("button", { name: /dex/i }).or(page.getByRole("link", { name: /dex/i }));
await login.or(button.first()).first().waitFor();
if (!(await login.isVisible())) await button.first().click();
// Форма dex: логин — почта.
await login.fill(email);
await page.locator("#password").fill(password);
await Promise.all([
  page.waitForURL((url) => url.origin === origin && !url.pathname.startsWith("/auth"), {
    timeout: 90_000,
  }),
  page.locator("#submit-login").click(),
]);

if (flag === "--api-key") {
  // Защита от CSRF (double submit): значение cookie __Host-csrfToken —
  // в заголовке x-csrf-token, запрос — из страницы, как у интерфейса.
  const result = await page.evaluate(async () => {
    const csrf = document.cookie
      .split("; ")
      .map((part) => part.split("="))
      .find(([name]) => /csrfToken$/.test(name));
    const response = await fetch("/api/apiKeys.create", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        ...(csrf ? { "x-csrf-token": csrf.slice(1).join("=") } : {}),
      },
      body: JSON.stringify({ name: "kronto-stand" }),
    });
    return { status: response.status, body: await response.text() };
  });
  if (result.status !== 200) throw new Error(`apiKeys.create: HTTP ${result.status} ${result.body}`);
  console.log(JSON.parse(result.body).data.value);
} else {
  console.log("ok");
}
await browser.close();
