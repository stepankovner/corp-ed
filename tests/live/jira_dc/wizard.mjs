// Мастер первого запуска Jira Software DC — в браузере (формы с токеном
// XSRF и переходами по шагам). Запуск из frontend/ — там Playwright:
//   cd frontend && JIRA_LICENSE=… node ../tests/live/jira_dc/wizard.mjs
// Переменные: JIRA_URL (http://127.0.0.1:8510), JIRA_LICENSE — тестовая
// лицензия, JIRA_ADMIN_PASSWORD; браузер — CHROMIUM_PATH (в песочнице
// Claude — /opt/pw-browsers/…/chrome). Печатает токен администратора
// последней строкой: ADMIN_TOKEN=…
import { createRequire } from "node:module";

// Playwright — из frontend/node_modules: скрипт запускают из frontend/.
const { chromium } = createRequire(`${process.cwd()}/package.json`)("playwright");

const base = (process.env.JIRA_URL || "http://127.0.0.1:8510").replace(/\/$/, "");
const license = process.env.JIRA_LICENSE || "";
const password = process.env.JIRA_ADMIN_PASSWORD || "admin-Test-1";
const browser = await chromium.launch(
  process.env.CHROMIUM_PATH ? { executablePath: process.env.CHROMIUM_PATH } : {},
);
const page = await browser.newPage();
page.setDefaultTimeout(300_000);

async function state() {
  const response = await page.request.get(`${base}/status`);
  return (await response.json()).state;
}

async function submit(selector) {
  const before = page.url();
  await Promise.all([
    page.waitForURL((url) => url.href !== before, { waitUntil: "domcontentloaded" }),
    page.locator(selector).first().click(),
  ]);
}

async function fields() {
  return page
    .locator("input:visible, textarea:visible, button:visible")
    .evaluateAll((els) => els.map((e) => `${e.tagName}#${e.id}[${e.name}]`));
}

// Шаг узнаётся по форме, а не по адресу: после создания администратора
// адрес остаётся SetupAdminAccount, а на странице уже настройка почты.
async function visible(selector) {
  return (await page.locator(selector).filter({ visible: true }).count()) > 0;
}

async function current() {
  if (!/Setup/i.test(page.url())) return "done";
  if (await visible("#jira-setupwizard-email-notifications-disabled")) return "mail";
  if (await visible("input[name=fullname]")) return "admin";
  if (await visible("#licenseKey")) return "license";
  if (await visible("input[name=baseURL]")) return "properties";
  if (await visible("[data-choice-value=classic]")) return "mode";
  return "unknown";
}

await page.goto(`${base}/`, { waitUntil: "domcontentloaded" });
let previous = "";
for (let step = 0; step < 12; step += 1) {
  await page.waitForLoadState("networkidle");
  const name = await current();
  console.log(`шаг ${step}: ${name} (${page.url()})`);
  if (name === "done") break;
  if (name === previous) {
    const errors = await page.locator(".error:visible, .aui-message-error:visible").allTextContents();
    throw new Error(`шаг не прошёл: ${name}; ${errors.join(" | ")}`);
  }
  previous = name;
  if (name === "mode") {
    // «Настрою сам»: база уже задана переменными образа.
    await page.locator("[data-choice-value=classic]").click();
    await submit("#jira-setup-mode-submit");
  } else if (name === "properties") {
    await page.locator("input[name=title]").fill("Kronto Jira");
    await page.locator("#jira-setupwizard-mode-private").check();
    await page.locator("input[name=baseURL]").fill(base);
    await submit("#jira-setupwizard-submit");
  } else if (name === "license") {
    if (!license) throw new Error("мастер просит лицензию: задайте JIRA_LICENSE");
    await page.locator("#licenseKey").fill(license);
    // Видимая кнопка — input «Next»; button#jira-setupwizard-submit скрыта.
    await submit("input[type=submit]:visible");
  } else if (name === "admin") {
    await page.locator("input[name=fullname]").fill("Admin Kronto");
    await page.locator("input[name=email]").fill("admin@example.com");
    await page.locator("input[name=username]").fill("admin");
    await page.locator("input[name=password]").fill(password);
    await page.locator("input[name=confirm]").fill(password);
    await submit("#jira-setupwizard-submit");
  } else if (name === "mail") {
    await page.locator("#jira-setupwizard-email-notifications-disabled").check();
    await submit("#jira-setupwizard-submit");
  } else {
    throw new Error(`неизвестный шаг ${page.url()}: ${(await fields()).join(" ")}`);
  }
}
console.log(`состояние: ${await state()}`);

// После мастера администратор вошёл. Токен — POST /rest/pat/latest/tokens
// из страницы (защита от XSRF сверяет Origin с адресом Jira).
await page.goto(`${base}/secure/Dashboard.jspa`, { waitUntil: "domcontentloaded" });
if (page.url().includes("login")) {
  await page.locator("#login-form-username, input[name=os_username]").first().fill("admin");
  await page.locator("#login-form-password, input[name=os_password]").first().fill(password);
  await Promise.all([
    page.waitForURL((url) => !url.pathname.includes("login")),
    page.locator("#login-form-submit, #login").first().click(),
  ]);
}
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
