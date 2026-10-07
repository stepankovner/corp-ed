import { describe, expect, it } from "vitest";

import { safeNext } from "./next";
import { passwordProblem } from "./password";

describe("passwordProblem", () => {
  const email = "anna.smirnova@meridian-stroy.ru";

  it("requires 12 characters", () => {
    expect(passwordProblem("короткий", "короткий", email)).toMatch(/12/);
  });

  it("rejects passwords containing the mailbox name", () => {
    expect(passwordProblem("anna.smirnova-2026", "anna.smirnova-2026", email)).toMatch(/почту/);
  });

  it("requires both entries to match", () => {
    expect(passwordProblem("длинная фраза один", "длинная фраза два", email)).toMatch(/совпадают/);
  });

  it("accepts a long passphrase", () => {
    expect(passwordProblem("длинная фраза для входа", "длинная фраза для входа", email)).toBeNull();
  });
});

describe("safeNext", () => {
  it.each([
    "/admin/users?x=1",
    "/c/42#answer",
    "/search?q=a%20b",
    "/search?q=100%25",
    "/settings/connections?status=ok",
    "/чат",
  ])("keeps own path %s", (value) => {
    expect(safeNext(value)).toBe(value);
  });

  it.each([
    null,
    "",
    "https://evil.example",
    "//evil.example",
    "javascript:alert(1)",
    "evil.example/x",
    // Браузер читает «\» как «/»: «/\» — это «//», другой сайт.
    "/\\evil.example",
    "\\\\evil.example",
    "/x\\y",
    // Таб, перевод строки и прочие управляющие браузер выбрасывает из адреса.
    "/\t/evil.example",
    "/\n/evil.example",
    "/x\u0000",
    "/x\u007f",
    // Закодированные «/», «\» и управляющие — в том числе дважды.
    "/%2F%2Fevil.example",
    "/%2f/evil.example",
    "/%5Cevil.example",
    "/%252F%252Fevil.example",
    "/%0A/evil.example",
    "/%E0%A4%A",
    // После разбора путь начинается с «//».
    "/..//evil.example",
    "/./%2E%2E//evil.example",
  ])("falls back to the root for %j", (value) => {
    expect(safeNext(value)).toBe("/");
  });
});
