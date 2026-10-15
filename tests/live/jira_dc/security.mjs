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
  await page.locator(".aui-page-panel-content").first().waitFor();
}

async function submit(selector) {
  await Promise.all([
    page.waitForLoadState("domcontentloaded"),
    page.waitForEvent("framenavigated"),
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

async function dump() {
  console.log(`${page.url()}`);
  console.log((await page.locator(".aui-page-panel-content").first().innerText()).slice(0, 1500));
  const links = await page
    .locator(".aui-page-panel-content a, .aui-page-panel-content input, .aui-page-panel-content form, .aui-page-panel-content select")
    .evaluateAll((els) =>
      els.map((e) => `${e.tagName} id=${e.id} name=${e.getAttribute("name") || ""} type=${e.getAttribute("type") || ""} href=${e.getAttribute("href") || e.getAttribute("action") || ""} text=${(e.innerText || e.value || "").trim().slice(0, 30)}`),
    );
  console.log(links.join("\n"));
}

await open("/secure/admin/ViewIssueSecuritySchemes.jspa");
let scheme = page.locator(".aui-page-panel-content a", { hasText: SCHEME }).first();
if (!(await scheme.count())) {
  await open("/secure/admin/AddIssueSecurityScheme!default.jspa");
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
    // Участник уже есть — повторный запуск его не дублирует.
    if ((await row.innerText()).includes(`(${user})`)) continue;
    await open(`/secure/admin/${await page.locator(`a[id="add_${level}"]`).getAttribute("href")}`);
    await page.locator("#user_id").check();
    await page.locator("#user").fill(user);
    await submit("#add_submit");
    await open(levels);
  }
}
console.log((await page.locator(".aui-page-panel-content table").first().innerText()).trim());

// Привязка к проекту — уже REST (PUT /project/{key} принимает id схемы).
const response = await page.request.put(`${base}/rest/api/2/project/ENG`, {
  data: { issueSecurityScheme: Number(schemeId) },
  headers: { "X-Atlassian-Token": "no-check" },
});
if (!response.ok()) throw new Error(`привязка схемы: HTTP ${response.status()} ${await response.text()}`);
console.log(`схема ${schemeId} привязана к ENG`);
await browser.close();
