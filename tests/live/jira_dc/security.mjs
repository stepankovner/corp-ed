// Схема уровней безопасности для проекта ENG — в браузере: REST Jira DC
// схемы и уровни только читает. Запуск из frontend/ после `seed.py rest`:
//   cd frontend && node ../tests/live/jira_dc/security.mjs
// Переменные: JIRA_URL, JIRA_ADMIN_PASSWORD, CHROMIUM_PATH — как у
// wizard.mjs. Имена и участники уровней — те же, что в seed.py
// (SECURITY_SCHEME, SECURITY_LEVELS); схема привязывается к ENG.
import { createRequire } from "node:module";

const { chromium } = createRequire(`${process.cwd()}/package.json`)("playwright");

const base = (process.env.JIRA_URL || "http://127.0.0.1:8510").replace(/\/$/, "");
const password = process.env.JIRA_ADMIN_PASSWORD || "admin-Test-1";
const SCHEME = "Kronto Секретность";
const LEVELS = {
  Руководство: ["carol", "svc-kronto", "admin"],
  "Без службы": ["carol", "admin"],
};

const browser = await chromium.launch(
  process.env.CHROMIUM_PATH ? { executablePath: process.env.CHROMIUM_PATH } : {},
);
const page = await browser.newPage();
page.setDefaultTimeout(30_000);

async function open(path) {
  await page.goto(`${base}${path}`, { waitUntil: "domcontentloaded" });
  // Страницы администрирования просят пароль ещё раз (WebSudo).
  const sudo = page.locator("#login-form-authenticatePassword");
  if (await sudo.count()) {
    await sudo.fill(password);
    await Promise.all([
      page.waitForURL((url) => !url.pathname.includes("WebSudo")),
      page.locator("#login-form-submit").click(),
    ]);
  }
  await page.waitForLoadState("networkidle");
  const content = page.locator(".aui-page-panel-content").first();
  await content.waitFor().catch(async (error) => {
    const text = (await page.locator("body").innerText()).slice(0, 400);
    throw new Error(`${page.url()} | ${await page.title()} | ${text} | ${error}`);
  });
}

async function submit(selector) {
  // Каждая форма здесь уводит на другой адрес (редирект после POST).
  const before = page.url();
  await Promise.all([
    page.waitForURL((url) => url.href !== before, { waitUntil: "domcontentloaded" }),
    page.locator(selector).first().click(),
  ]);
  await page.waitForLoadState("networkidle");
}

// Вход: в 10.x форма — #username-field, раньше — #login-form-username.
await page.goto(`${base}/login.jsp`, { waitUntil: "domcontentloaded" });
await page.locator("#username-field, #login-form-username").first().fill("admin");
await page.locator("#password-field, #login-form-password").first().fill(password);
await Promise.all([
  page.waitForURL((url) => !url.pathname.includes("login")),
  page.locator("#login-button, #login-form-submit").first().click(),
]);

await open("/secure/admin/ViewIssueSecuritySchemes.jspa");
let scheme = page.locator(".aui-page-panel-content a", { hasText: SCHEME }).first();
if (!(await scheme.count())) {
  // Ссылка со страницы списка — с atl_token: прямой переход без него Jira
  // 10.3 уводит на вход («permissionViolation»).
  await submit("#add_securityscheme");
  await page.locator("input[name=name]").fill(SCHEME);
  await submit("#add_submit");
  scheme = page.locator(".aui-page-panel-content a", { hasText: SCHEME }).first();
}
const schemeId = new URL(await scheme.getAttribute("href"), page.url()).searchParams.get("schemeId");
console.log(`SCHEME_ID=${schemeId}`);
const levels = `/secure/admin/EditIssueSecurities!default.jspa?schemeId=${schemeId}`;
await open(levels);
for (const [level, users] of Object.entries(LEVELS)) {
  if ((await page.locator(`a[id="add_${level}"]`).count()) === 0) {
    await page.locator("form[action*=addLevel] input[name=name]").fill(level);
    await submit("#level_submit");
  }
  const row = page.locator(".aui-page-panel-content tr", { hasText: level }).first();
  for (const user of users) {
    // Участник уже есть (в строке — его отображаемое имя; у тестовых
    // учёток оно совпадает с логином) — повторный запуск его не дублирует.
    if ((await row.innerText()).toLowerCase().includes(`(${user})`)) continue;
    await open(`/secure/admin/${await page.locator(`a[id="add_${level}"]`).getAttribute("href")}`);
    await page.locator("#user_id").check();
    await page.locator("#user").fill(user);
    await submit("#add_submit");
    await open(levels);
  }
}
for (const level of Object.keys(LEVELS)) {
  const row = page.locator(".aui-page-panel-content tr", { hasText: level }).first();
  console.log((await row.innerText()).replace(/\s+/g, " ").trim());
}

// Привязка к проекту — уже REST (PUT /project/{key} принимает id схемы).
const response = await page.request.put(`${base}/rest/api/2/project/ENG`, {
  data: { issueSecurityScheme: Number(schemeId) },
  headers: { "X-Atlassian-Token": "no-check" },
});
if (!response.ok()) throw new Error(`привязка схемы: HTTP ${response.status()} ${await response.text()}`);
console.log(`схема ${schemeId} привязана к ENG`);
await browser.close();
