// Персональный токен доступа пользователя Confluence: вход в браузере и
// POST /rest/pat/latest/tokens из страницы (защита от XSRF сверяет Origin).
// Запуск из frontend/: node ../tests/live/confluence_dc/token.mjs <логин> <пароль>
// Печатает токен одной строкой. CONFLUENCE_URL, CHROMIUM_PATH — как у wizard.mjs.
import { createRequire } from "node:module";

const { chromium } = createRequire(`${process.cwd()}/package.json`)("playwright");
const base = (process.env.CONFLUENCE_URL || "http://127.0.0.1:8090").replace(/\/$/, "");
const [username, password] = process.argv.slice(2);
if (!username || !password) throw new Error("нужны логин и пароль");

const browser = await chromium.launch(
  process.env.CHROMIUM_PATH ? { executablePath: process.env.CHROMIUM_PATH } : {},
);
const page = await browser.newPage();
page.setDefaultTimeout(120_000);
await page.goto(`${base}/login.action`, { waitUntil: "networkidle" });
// Форма входа: в 7.x–9.x — #os_username, в 10.x — #username-field.
await page.locator("#os_username, #username-field").first().fill(username);
await page.locator("#os_password, #password-field").first().fill(password);
await Promise.all([
  page.waitForURL((url) => !url.pathname.endsWith("/login.action")),
  page.locator("#loginButton, #login-button").first().click(),
]);
const result = await page.evaluate(async (name) => {
  const response = await fetch("/rest/pat/latest/tokens", {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-Atlassian-Token": "no-check" },
    body: JSON.stringify({ name, expirationDuration: 1 }),
  });
  return { status: response.status, body: await response.text() };
}, `kronto-${username}-${Date.now()}`);
if (result.status !== 200 && result.status !== 201) {
  throw new Error(`токен: HTTP ${result.status} ${result.body}`);
}
console.log(JSON.parse(result.body).rawToken);
await browser.close();
