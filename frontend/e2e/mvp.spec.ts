import { expect, test, type BrowserContext, type Cookie, type Page } from "@playwright/test";

import { codeFrom, env, linkFrom, nextTotp, waitForMail } from "./support";

/**
 * Путь целиком: администратор входит с кодом приложения и запоминает
 * устройство, задаёт свой пароль, загружает документ и получает ответ со
 * ссылкой на источник, создаёт приглашение; новый сотрудник регистрируется
 * по ссылке, подтверждает почту кодом из письма и вступает; второй —
 * вступает по коду с главной; пароль восстанавливается по ссылке из
 * письма; посетитель записывается на созвон со страницы тарифов.
 *
 * Письма читаются из перехватчика Mailpit (MAILPIT_URL), коды
 * приложения считаются по секрету из `cli set-totp` (E2E_TOTP_SECRET).
 */

const adminEmail = env("E2E_ADMIN_EMAIL");
const adminTemporary = env("E2E_ADMIN_PASSWORD");
const totpSecret = env("E2E_TOTP_SECRET");
const adminPassword = "сквозная проверка админа 2026";
const employeePassword = "сквозная проверка сотрудника 2026";
const run = Date.now();
const invitedEmail = `invited-${run}@kronto-e2e.ru`;
const coderEmail = `coder-${run}@kronto-e2e.ru`;
const codeWord = `пароль-${Math.random().toString(36).slice(2, 8)}`;
const leadCompany = `E2E Лид ${run}`;

const DOCUMENT = `# Положение о командировках

## Суточные

Суточные при командировках по России составляют 700 рублей в сутки.
Кодовое слово для проверки: ${codeWord}.

## Отчёт

Авансовый отчёт сдаётся в течение трёх рабочих дней после возвращения.
`;

let inviteUrl = "";
let inviteCode = "";
/** Cookie доверенного устройства администратора: дальше вход без второго шага. */
let adminDevice: Cookie | null = null;

/** Ближайший будний день после сегодняшнего по Москве, YYYY-MM-DD. */
function nextWorkday(): string {
  const moscow = new Date(Date.now() + 3 * 3600_000);
  const day = new Date(
    Date.UTC(moscow.getUTCFullYear(), moscow.getUTCMonth(), moscow.getUTCDate()),
  );
  do day.setUTCDate(day.getUTCDate() + 1);
  while (day.getUTCDay() === 0 || day.getUTCDay() === 6);
  return day.toISOString().slice(0, 10);
}

const INSIDE = (page: Page) =>
  page
    .getByRole("log", { name: "Переписка" })
    .or(page.getByRole("heading", { name: "Задайте свой пароль" }))
    .or(page.getByRole("heading", { name: /^Здравствуйте,/ }))
    .first();

async function submitPassword(page: Page, email: string, password: string, remember = false) {
  await page.goto("/login");
  await page.getByLabel("Почта").fill(email);
  await page.getByLabel("Пароль", { exact: true }).fill(password);
  if (remember) await page.getByLabel("Запомнить это устройство на 30 дней").check();
  await page.getByRole("button", { name: "Войти" }).click();
}

/**
 * Вход администратора: код приложения, если устройство ещё не доверенное.
 * remember — запомнить устройство: дальше вход без второго шага.
 */
async function loginAdmin(
  page: Page,
  context: BrowserContext,
  password = adminPassword,
  remember = true,
) {
  if (adminDevice) await context.addCookies([adminDevice]);
  const trusted = adminDevice !== null;
  await submitPassword(page, adminEmail, password, remember && !trusted);
  if (!trusted) {
    await expect(page.getByRole("heading", { name: "Код из приложения" })).toBeVisible();
    // Шесть цифр уходят сами.
    await page.getByLabel("Код из 6 цифр").fill(await nextTotp(totpSecret));
  }
  // Дождаться конца входа: переход по адресу раньше прервал бы запрос.
  await expect(INSIDE(page)).toBeVisible();
  if (!trusted && remember) {
    adminDevice = (await context.cookies()).find((c) => c.name === "kronto_device") ?? null;
    expect(adminDevice).not.toBeNull();
  }
}

/** Вход сотрудника без приложения: код из письма. */
async function loginByMail(page: Page, email: string, password: string) {
  const since = Date.now();
  await submitPassword(page, email, password);
  await expect(page.getByRole("heading", { name: "Код из письма" })).toBeVisible();
  const mail = await waitForMail(email, since, /Код для входа/);
  await page.getByLabel("Код из 6 цифр").fill(codeFrom(mail.subject));
  await expect(INSIDE(page)).toBeVisible();
}

/** Регистрация и подтверждение почты кодом из письма. */
async function register(page: Page, email: string, firstName: string) {
  const since = Date.now();
  await page.getByLabel("Имя").fill(firstName);
  await page.getByLabel("Фамилия").fill("Проверкина");
  await page.getByLabel("Почта").fill(email);
  await page.getByLabel("Пароль", { exact: true }).fill(employeePassword);
  await page.getByLabel("Повторите пароль").fill(employeePassword);
  await page.getByRole("checkbox", { name: /Соглашаюсь на обработку/ }).check();
  await page.getByRole("button", { name: "Зарегистрироваться" }).click();
  await expect(page.getByRole("heading", { name: "Подтвердите почту" })).toBeVisible();
  const mail = await waitForMail(email, since, /Код подтверждения/);
  await page.getByLabel("Код из 6 цифр").fill(codeFrom(mail.subject));
}

async function changePassword(page: Page, temporary: string, next: string) {
  await expect(page.getByRole("heading", { name: "Задайте свой пароль" })).toBeVisible();
  await page.getByLabel("Временный пароль").fill(temporary);
  await page.getByLabel("Новый пароль", { exact: true }).fill(next);
  await page.getByLabel("Повторите новый пароль").fill(next);
  await page.getByRole("button", { name: "Сохранить пароль" }).click();
  await expect(page.getByRole("log", { name: "Переписка" })).toBeVisible();
}

async function ask(page: Page, question: string) {
  await page.getByLabel("Ваш вопрос").fill(question);
  await page.getByRole("button", { name: "Отправить вопрос" }).click();
}

test.describe.serial("путь компании", () => {
  test("администратор: код приложения, свой пароль, доверенное устройство", async ({
    page,
    context,
  }) => {
    await loginAdmin(page, context, adminTemporary, false);
    await changePassword(page, adminTemporary, adminPassword);
    await expect(page.getByRole("link", { name: "Управление" })).toBeVisible();

    // Смена пароля закрыла сеансы и забыла доверенные устройства —
    // входим заново и запоминаем это.
    await context.clearCookies();
    await loginAdmin(page, context);
    expect(adminDevice).toMatchObject({ httpOnly: true, path: "/api/v1/auth" });
    await context.clearCookies();
    // Доверенное устройство входит без второго шага.
    await loginAdmin(page, context);
    await expect(page.getByRole("log", { name: "Переписка" })).toBeVisible();
  });

  test("сессия переживает перезагрузку, токены — не в localStorage", async ({ page, context }) => {
    await loginAdmin(page, context);
    // Refresh-токен — httpOnly-cookie только для ручек входа (RISKS №44).
    const cookies = await context.cookies();
    const refresh = cookies.find((cookie) => cookie.name === "kronto_refresh");
    expect(refresh).toMatchObject({ httpOnly: true, sameSite: "Strict", path: "/api/v1/auth" });
    const stored = await page.evaluate(() => JSON.stringify(Object.entries(localStorage)));
    expect(stored).not.toContain(refresh?.value ?? "no-cookie");
    expect(stored).not.toMatch(/eyJ[\w-]+\./); // ни одного JWT

    await page.reload();
    await expect(page.getByRole("log", { name: "Переписка" })).toBeVisible();

    await page.getByRole("button", { name: /^Профиль/ }).click();
    await page.getByRole("menuitem", { name: "Выйти" }).click();
    await expect(page).toHaveURL(/\/login/);
    await page.reload();
    await expect(page.getByLabel("Почта")).toBeVisible();
  });

  test("оболочка: тема переживает перезагрузку, заголовки вкладок, меню на телефоне", async ({
    page,
    context,
  }) => {
    await loginAdmin(page, context);
    await expect(page).toHaveTitle("Вопросы — kronto");

    await page.getByRole("button", { name: /^Профиль/ }).click();
    await page.getByRole("menuitemradio", { name: "Тёмная" }).click();
    await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");
    await page.keyboard.press("Escape");
    // Тему ставит скрипт в index.html до загрузки приложения.
    await page.reload();
    await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");
    await expect(page.locator('meta[name="theme-color"]')).toHaveAttribute("content", "#1c1b19");

    await page.getByRole("link", { name: "Управление" }).click();
    await expect(page).toHaveTitle("Документы — kronto");

    await page.setViewportSize({ width: 390, height: 844 });
    await page.getByRole("button", { name: "Открыть меню" }).click();
    const drawer = page.getByRole("dialog", { name: "Меню" });
    await drawer.getByRole("link", { name: "Сотрудники" }).click();
    await expect(page.getByRole("heading", { name: "Сотрудники" })).toBeVisible();
    await expect(drawer).toHaveCount(0);
    await expect(page).toHaveTitle("Сотрудники — kronto");
    // Горизонтальной прокрутки страницы на телефоне нет.
    const overflow = await page.evaluate<number>(
      "document.documentElement.scrollWidth - document.documentElement.clientWidth",
    );
    expect(overflow).toBe(0);
  });

  test("загруженный документ индексируется", async ({ page, context }) => {
    await loginAdmin(page, context);
    await page.getByRole("link", { name: "Управление" }).click();
    await expect(page.getByRole("heading", { name: "Документы" })).toBeVisible();
    await page.locator("input[type=file]").setInputFiles({
      name: "komandirovki.md",
      mimeType: "text/markdown",
      buffer: Buffer.from(DOCUMENT, "utf8"),
    });
    const row = page.getByRole("row").filter({ hasText: "komandirovki" });
    await expect(row.getByText("готов", { exact: true })).toBeVisible({ timeout: 60_000 });
  });

  test("ответ ссылается на документ и открывает фрагмент", async ({ page, context }) => {
    await loginAdmin(page, context);
    await ask(page, `Какое кодовое слово в положении о командировках, суточные ${codeWord}?`);
    const answer = page.getByRole("log").locator("div").filter({ hasText: codeWord }).last();
    await expect(answer).toBeVisible({ timeout: 30_000 });
    await page
      .getByRole("button", { name: /^Источник 1: / })
      .first()
      .click();
    const panel = page.getByRole("dialog");
    await expect(panel.getByRole("heading", { name: "komandirovki" })).toBeVisible();
    await expect(panel.getByText(codeWord)).toBeVisible();
    await page.keyboard.press("Escape");
    await expect(panel).toBeHidden();

    await page.getByRole("button", { name: "Ответ помог" }).click();
    await expect(page.getByText("Спасибо за оценку")).toBeVisible();
  });

  // Новая компания — в строгом режиме (решение 28.09): честный отказ.
  // Вид общего ответа проверяют юнит-тесты (App.test.tsx).
  test("вопрос мимо документов — общий ответ с пометкой", async ({ page, context }) => {
    // Режим новой компании по умолчанию (решение 29.09, BH-29): первая
    // строка и плашка говорят, что ответ не из документов.
    await loginAdmin(page, context);
    await ask(page, "Какая столица Австралии?");
    await expect(page.getByText("В документах компании ответа нет").first()).toBeVisible({
      timeout: 30_000,
    });
    await expect(page.getByRole("button", { name: /^Источник 1: / })).toHaveCount(0);
  });

  test("администратор создаёт приглашение: ссылка и код", async ({ page, context }) => {
    await loginAdmin(page, context);
    await page.goto("/admin/users");
    await page.getByRole("button", { name: "Пригласить" }).click();
    await page.getByRole("button", { name: "Создать приглашение" }).click();
    const created = page.getByRole("dialog", { name: "Приглашение" });
    inviteUrl = (await created.getByTestId("invite-link").textContent())?.trim() ?? "";
    inviteCode = (await created.getByTestId("invite-code").textContent())?.trim() ?? "";
    expect(inviteUrl).toMatch(/\/join#[\w-]{40,}$/);
    expect(inviteCode).toMatch(/^[0-9A-Z]{4}-[0-9A-Z]{4}$/);
    await created.getByRole("button", { name: "Готово" }).click();
    await expect(page.getByRole("table", { name: "Приглашения" })).toContainText("действует");
  });

  test("новый сотрудник: регистрация по ссылке, код из письма, вступление", async ({ page }) => {
    await page.goto(inviteUrl);
    await expect(page.getByRole("heading", { name: "Присоединиться к команде" })).toBeVisible();
    // Секрет приглашения не остаётся в адресной строке.
    await expect(page).toHaveURL(/\/join$/);
    await page.getByRole("link", { name: "Создать учётную запись" }).click();
    await register(page, invitedEmail, "Инна");

    await page.getByRole("button", { name: "Вступить" }).click();
    await expect(page.getByRole("log", { name: "Переписка" })).toBeVisible();
    await expect(page.getByRole("link", { name: "Управление" })).toHaveCount(0);
    await ask(page, `Сколько суточных по России? ${codeWord}`);
    await expect(page.getByRole("button", { name: /^Источник 1: / }).first()).toBeVisible({
      timeout: 30_000,
    });

    // В управление сотрудника не пускает.
    await page.goto("/admin/users");
    await expect(page).toHaveURL(/\/$/);
  });

  test("учётка без компании вступает по коду с главной", async ({ page }) => {
    await page.goto("/register");
    await register(page, coderEmail, "Кирилл");
    await expect(page.getByRole("heading", { name: "Здравствуйте, Кирилл" })).toBeVisible();
    await page.getByLabel("Ссылка или код приглашения").fill(inviteCode.toLowerCase());
    await page.getByRole("button", { name: "Продолжить" }).click();
    await page.getByRole("button", { name: "Вступить" }).click();
    await expect(page.getByRole("log", { name: "Переписка" })).toBeVisible();
    await expect(page.getByRole("button", { name: /^Компания: E2E/ })).toBeVisible();
  });

  test("пароль восстанавливается по ссылке, вход с нового устройства — код из письма", async ({
    page,
  }) => {
    const since = Date.now();
    await page.goto("/forgot-password");
    await page.getByLabel("Почта").fill(invitedEmail);
    await page.getByRole("button", { name: "Отправить ссылку" }).click();
    await expect(page.getByText(/Если учётная запись с адресом/)).toBeVisible();
    const mail = await waitForMail(invitedEmail, since, /Восстановление пароля/);

    const fresh = `${employeePassword} новый`;
    await page.goto(linkFrom(mail.text, "/reset-password"));
    await page.getByLabel("Новый пароль", { exact: true }).fill(fresh);
    await page.getByLabel("Повторите пароль").fill(fresh);
    await page.getByRole("button", { name: "Сохранить и войти" }).click();
    await expect(page.getByRole("log", { name: "Переписка" })).toBeVisible();

    await page.getByRole("button", { name: /^Профиль/ }).click();
    await page.getByRole("menuitem", { name: "Выйти" }).click();
    await loginByMail(page, invitedEmail, fresh);
    await expect(page.getByRole("log", { name: "Переписка" })).toBeVisible();
  });

  test("посетитель выбирает тариф и записывается на созвон", async ({ page }) => {
    await page.goto("/pricing");
    await page
      .getByRole("region", { name: "Базовый" })
      .getByRole("link", { name: "Записаться на созвон" })
      .click();
    await page.getByLabel("Компания").fill(leadCompany);
    await page.getByLabel("Сколько сотрудников работают за компьютером").fill("60");
    await page.getByLabel("Как к вам обращаться").fill("Анна");
    await page.getByLabel("Телефон").fill("+7 999 123-45-67");
    await page.getByLabel("Удобная дата").fill(nextWorkday());
    await page.getByLabel("Удобное время (по Москве)").selectOption({ index: 1 });
    await page.getByRole("checkbox", { name: /Согласен на обработку/ }).check();
    await page.getByRole("button", { name: "Отправить заявку" }).click();
    await expect(page.getByText("Заявка отправлена")).toBeVisible();
  });
});
