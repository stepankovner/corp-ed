// Вход сотрудника через OAuth2 Nextcloud в браузере: открыть адрес
// авторизации, войти, «Предоставить доступ» и поймать возврат на адрес
// клиента — печатает код авторизации одной строкой.
// Запуск из frontend/:
//   node ../tests/live/nextcloud/oauth.mjs <адрес авторизации> <адрес возврата> <логин> <пароль>
// CHROMIUM_PATH — как у ../confluence_dc/wizard.mjs. Сертификат стенда
// самоподписанный — браузер стенда его не проверяет (только 127.0.0.1).
import { createRequire } from "node:module";

const { chromium } = createRequire(`${process.cwd()}/package.json`)("playwright");
const [authorizeUrl, callback, username, password] = process.argv.slice(2);
if (!authorizeUrl || !callback || !username || !password) {
  throw new Error("нужны адрес авторизации, адрес возврата, логин и пароль");
}

const browser = await chromium.launch(
  process.env.CHROMIUM_PATH ? { executablePath: process.env.CHROMIUM_PATH } : {},
);
const context = await browser.newContext({ ignoreHTTPSErrors: true });
const page = await context.newPage();
page.setDefaultTimeout(60_000);

let returned = null;
// Возврат на адрес клиента не открываем: нужен только код из адреса.
await context.route(`${callback}**`, async (route) => {
  returned = route.request().url();
  await route.fulfill({ status: 200, contentType: "text/plain", body: "ok" });
});

await page.goto(authorizeUrl);
const deadline = Date.now() + 90_000;
while (!returned && Date.now() < deadline) {
  // Шаги потока входа Nextcloud: «Войти» → логин и пароль → «Предоставить доступ».
  const user = page.locator("#user");
  if (await user.isVisible().catch(() => false)) {
    await user.fill(username);
    await page.locator("#password").fill(password);
    await page.locator("button[type=submit], input[type=submit]").first().click();
    await page.waitForLoadState("networkidle").catch(() => {});
    continue;
  }
  const button = page
    .getByRole("button", { name: /log in|grant access|войти|предоставить доступ|разрешить/i })
    .or(page.getByRole("link", { name: /log in|войти/i }))
    .first();
  if (await button.isVisible().catch(() => false)) {
    await button.click();
    await page.waitForLoadState("networkidle").catch(() => {});
    continue;
  }
  await page.waitForTimeout(500);
}
await browser.close();
if (!returned) throw new Error("возврата на адрес клиента не было");
const code = new URL(returned).searchParams.get("code");
if (!code) throw new Error("в возврате нет кода");
console.log(code);
