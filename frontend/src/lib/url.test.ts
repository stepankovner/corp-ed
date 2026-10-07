import { describe, expect, it } from "vitest";

import { safeHttpUrl, safeLinkHref } from "./url";

const UNSAFE = [
  "javascript:alert(1)",
  "JaVaScRiPt:alert(1)",
  " javascript:alert(1)",
  "java\tscript:alert(1)",
  "data:text/html,<script>alert(1)</script>",
  "vbscript:msgbox(1)",
  "file:///etc/passwd",
  "blob:https://kronto.example/1",
  "mailto:anna@example.ru",
];

describe("safeHttpUrl", () => {
  it.each(["https://portal.example.ru/docs/42", "http://localhost:8000/oauth?x=1"])(
    "пропускает %s",
    (value) => {
      expect(safeHttpUrl(value)).toBe(new URL(value).href);
    },
  );

  it.each([null, undefined, "", "/privacy", "//evil.example", ...UNSAFE])(
    "не пропускает %j",
    (value) => {
      expect(safeHttpUrl(value)).toBeNull();
    },
  );
});

describe("safeLinkHref", () => {
  it("свой путь — как есть", () => {
    expect(safeLinkHref("/privacy")).toBe("/privacy");
  });

  it("http(s)-адрес — разобранным", () => {
    expect(safeLinkHref("https://krontoai.ru/privacy")).toBe("https://krontoai.ru/privacy");
  });

  it.each([null, undefined, "", "/\\evil.example", "privacy", ...UNSAFE])(
    "не пропускает %j",
    (value) => {
      expect(safeLinkHref(value)).toBeNull();
    },
  );
});
