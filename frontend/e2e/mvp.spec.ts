import { expect, test, type Page } from "@playwright/test";

/**
 * Путь MVP целиком: администратор входит по временному паролю, загружает
 * документ, получает ответ со ссылкой на источник, заводит сотрудника;
 * сотрудник входит и спрашивает сам.
 */

function env(name: string): string {
  const value = process.env[name];
  if (!value) throw new Error(`${name} is required (see playwright.config.ts)`);
  return value;
}

const company = env("E2E_COMPANY");
const adminEmail = env("E2E_ADMIN_EMAIL");
const adminTemporary = env("E2E_ADMIN_PASSWORD");
const adminPassword = "сквозная проверка админа 2026";
const employeeEmail = `employee-${Date.now()}@kronto-e2e.ru`;
const employeePassword = "сквозная проверка сотрудника 2026";
const codeWord = `пароль-${Math.random().toString(36).slice(2, 8)}`;

const DOCUMENT = `# Положение о командировках

## Суточные

Суточные при командировках по России составляют 700 рублей в сутки.
Кодовое слово для проверки: ${codeWord}.

## Отчёт

Авансовый отчёт сдаётся в течение трёх рабочих дней после возвращения.
`;

let employeeTemporary = "";

async function login(page: Page, email: string, password: string) {
  await page.goto("/login");
  await page.getByLabel("Код компании").fill(company);
  await page.getByLabel("Рабочая почта").fill(email);
  await page.getByLabel("Пароль").fill(password);
  await page.getByRole("button", { name: "Войти" }).click();
  // Дождаться конца входа: переход по адресу раньше прервал бы запрос.
  await expect(
    page
      .getByRole("log", { name: "Переписка" })
      .or(page.getByRole("heading", { name: "Задайте свой пароль" })),
  ).toBeVisible();
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

test.describe.serial("MVP", () => {
  test("администратор задаёт свой пароль при первом входе", async ({ page }) => {
    await login(page, adminEmail, adminTemporary);
    await changePassword(page, adminTemporary, adminPassword);
    await expect(page.getByRole("link", { name: "Управление" })).toBeVisible();
  });

  test("загруженный документ индексируется", async ({ page }) => {
    await login(page, adminEmail, adminPassword);
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

  test("ответ ссылается на документ и открывает фрагмент", async ({ page }) => {
    await login(page, adminEmail, adminPassword);
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
  test("вопрос мимо документов — честный отказ", async ({ page }) => {
    await login(page, adminEmail, adminPassword);
    await ask(page, "Какая столица Австралии?");
    await expect(page.getByText("В документах компании нет ответа на этот вопрос.")).toBeVisible({
      timeout: 30_000,
    });
  });

  test("администратор заводит сотрудника", async ({ page }) => {
    await login(page, adminEmail, adminPassword);
    await page.goto("/admin/users");
    await page.getByRole("button", { name: "Добавить сотрудника" }).click();
    await page.getByLabel("Рабочая почта").fill(employeeEmail);
    await page.getByRole("button", { name: "Добавить", exact: true }).click();
    const secret = page.getByRole("dialog", { name: "Временный пароль" }).locator("code");
    employeeTemporary = (await secret.textContent())?.trim() ?? "";
    expect(employeeTemporary.length).toBeGreaterThan(8);
    await page.getByRole("button", { name: "Готово" }).click();
    await expect(page.getByRole("row").filter({ hasText: employeeEmail })).toContainText(
      "временный пароль",
    );
  });

  test("сотрудник входит, спрашивает и не видит управления", async ({ page }) => {
    await login(page, employeeEmail, employeeTemporary);
    await changePassword(page, employeeTemporary, employeePassword);
    await expect(page.getByRole("link", { name: "Управление" })).toHaveCount(0);
    await ask(page, `Сколько суточных по России? ${codeWord}`);
    await expect(page.getByRole("button", { name: /^Источник 1: / }).first()).toBeVisible({
      timeout: 30_000,
    });

    await page.goto("/admin/users");
    await expect(page).toHaveURL(/\/$/);

    await page.getByRole("button", { name: "Профиль" }).click();
    await page.getByRole("menuitem", { name: "Выйти" }).click();
    await expect(page).toHaveURL(/\/login/);
  });
});
