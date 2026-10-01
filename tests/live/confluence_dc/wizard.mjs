// Мастер первого запуска Confluence DC — в браузере (он собран на JS, формы
// одной отправкой не пройти). Запуск из frontend/ — там Playwright:
//   cd frontend && node ../tests/live/confluence_dc/wizard.mjs
// Переменные: CONFLUENCE_URL (http://127.0.0.1:8090), CONFLUENCE_LICENSE —
// если мастер спросит лицензию, CONFLUENCE_ADMIN_PASSWORD; браузер —
// CHROMIUM_PATH (в песочнице Claude — /opt/pw-browsers/…/chrome).
import { createRequire } from "node:module";

// Playwright — из frontend/node_modules: скрипт запускают из frontend/.
const { chromium } = createRequire(`${process.cwd()}/package.json`)("playwright");

const base = (process.env.CONFLUENCE_URL || "http://127.0.0.1:8090").replace(/\/$/, "");
const license = process.env.CONFLUENCE_LICENSE || "";
const password = process.env.CONFLUENCE_ADMIN_PASSWORD || "admin-Test-1";
const browser = await chromium.launch(
  process.env.CHROMIUM_PATH ? { executablePath: process.env.CHROMIUM_PATH } : {},
);
const page = await browser.newPage();
page.setDefaultTimeout(180_000);

async function state() {
  const response = await page.request.get(`${base}/status`);
  return (await response.json()).state;
}

async function click(name) {
  // «Далее» в мастере — то <button>, то <input type=submit id=setup-next-button>.
  const next = page.locator("#setup-next-button:visible");
  const target =
    name === "Next" && (await next.count()) > 0
      ? next.first()
      : page.getByRole("button", { name }).first();
  await Promise.all([page.waitForLoadState("domcontentloaded"), target.click()]);
}

async function errors() {
  const found = await page
    .locator(".error:visible, .aui-message-error:visible, .field-error:visible")
    .allTextContents();
  return found.map((text) => text.trim()).filter(Boolean);
}

await page.goto(`${base}/`, { waitUntil: "domcontentloaded" });
let previous = "";
for (let step = 0; step < 15; step += 1) {
  await page.waitForTimeout(1500);
  if ((await state()) === "RUNNING" && !page.url().includes("/setup/")) break;
  const title = await page.title();
  console.log(`шаг ${step}: ${title}`);
  if (title === previous) {
    throw new Error(`шаг не прошёл: ${title}; ошибки: ${(await errors()).join(" | ")}`);
  }
  previous = title;
  if (/deployment type/i.test(title)) {
    await page.locator("#clusteringDisabled").check();
    await click("Next");
  } else if (/licen/i.test(title)) {
    if (!license) throw new Error("мастер просит лицензию: задайте CONFLUENCE_LICENSE");
    await page.locator("textarea").first().fill(license);
    await click("Next");
  } else if (/content/i.test(title)) {
    await click(/empty site/i);
  } else if (/user management/i.test(title)) {
    await click(/within Confluence/i);
  } else if (/administrator/i.test(title)) {
    await page.locator("#username, input[name=username]").first().fill("admin");
    await page.locator("#fullName, input[name=fullName]").first().fill("Admin Kronto");
    await page.locator("#email, input[name=email]").first().fill("admin@example.com");
    await page.locator("#password, input[name=password]").first().fill(password);
    await page.locator("#confirm, input[name=confirm]").first().fill(password);
    await click("Next");
  } else if (/success|all set|setup complete/i.test(title)) {
    break;
  } else {
    const buttons = await page.locator("button:visible").allTextContents();
    throw new Error(`неизвестный шаг «${title}», кнопки: ${buttons.join(" | ")}`);
  }
}
console.log(`состояние: ${await state()}`);

// Базовая авторизация REST в Confluence 10 по умолчанию выключена — дальше
// нужен персональный токен. Выдаём его администратору через сессию браузера
// и печатаем последней строкой: его читает seed.py.
await page.goto(`${base}/login.action`, { waitUntil: "networkidle" });
// После мастера администратор уже вошёл — формы входа нет. Форма: в
// 7.x–9.x — #os_username, в 10.x — #username-field.
const user = page.locator("#os_username, #username-field").first();
if (await user.count()) {
  await user.fill("admin");
  await page.locator("#os_password, #password-field").first().fill(password);
  await Promise.all([
    page.waitForURL((url) => !url.pathname.endsWith("/login.action")),
    page.locator("#loginButton, #login-button").first().click(),
  ]);
}
// 7.x: пользователей и группы REST не ведёт — seed.py идёт в JSON-RPC, а
// тот требует «безопасного сеанса администратора» (WebSudo). На стенде
// его выключаем; в 8.x+ seed.py обходится REST, форма та же — не мешает.
await page.goto(`${base}/admin/editsecurityconfig.action`, { waitUntil: "networkidle" });
if (page.url().includes("/authenticate.action")) {
  await page.locator("#password").fill(password);
  await Promise.all([
    page.waitForURL((url) => !url.pathname.endsWith("/authenticate.action")),
    page.locator("#authenticateButton").click(),
  ]);
  await page.waitForLoadState("networkidle");
}
const webSudo = page.locator("#webSudoEnabled");
if ((await webSudo.count()) && (await webSudo.isChecked())) {
  // Подпись «Enable» перекрывает флажок — щелчок силой.
  await webSudo.uncheck({ force: true });
  await Promise.all([page.waitForLoadState("domcontentloaded"), page.locator("#confirm").click()]);
  console.log("WebSudo выключен");
}

// Запрос — из самой страницы: защита от XSRF сверяет Origin с адресом
// Confluence, внешний клиент с сессионной кукой она не пускает.
const result = await page.evaluate(async (name) => {
  const response = await fetch("/rest/pat/latest/tokens", {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-Atlassian-Token": "no-check" },
    body: JSON.stringify({ name, expirationDuration: 1 }),
  });
  return { status: response.status, body: await response.text() };
}, `kronto-check-${Date.now()}`);
if (result.status !== 200 && result.status !== 201) {
  throw new Error(`токен: HTTP ${result.status} ${result.body}`);
}
console.log(`ADMIN_TOKEN=${JSON.parse(result.body).rawToken}`);
await browser.close();
