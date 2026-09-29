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
  it("keeps own paths", () => {
    expect(safeNext("/admin/users?x=1")).toBe("/admin/users?x=1");
  });

  it.each([null, "", "https://evil.example", "//evil.example", "javascript:alert(1)"])(
    "falls back to the root for %s",
    (value) => {
      expect(safeNext(value)).toBe("/");
    },
  );
});
