import { describe, expect, it } from "vitest";

import tokens from "./tokens.css?raw";

const styles = import.meta.glob<string>("../**/*.css", {
  query: "?raw",
  import: "default",
  eager: true,
});

/** Объявления блока без комментариев и лишних пробелов. */
function declarations(block: string): string[] {
  return block
    .replace(/\/\*[\s\S]*?\*\//g, "")
    .split(";")
    .map((line) => line.replace(/\s+/g, " ").trim())
    .filter(Boolean);
}

/** Тело первого блока после метки: селектора с «{» или комментария над блоком. */
function blockAfter(marker: string): string {
  const start = tokens.indexOf(marker);
  expect(start, marker).toBeGreaterThan(-1);
  const open = tokens.indexOf("{", start);
  const close = tokens.indexOf("}", open);
  return tokens.slice(open + 1, close);
}

const names = (block: string) =>
  declarations(block)
    .map((line) => line.split(":")[0] ?? "")
    .filter((name) => name.startsWith("--"));

describe("токены оформления", () => {
  it("тёмная тема по атрибуту и по системе — один и тот же набор", () => {
    const byAttribute = declarations(blockAfter(':root[data-theme="dark"] {'));
    const bySystem = declarations(blockAfter(':root:not([data-theme="light"]) {'));
    expect(byAttribute.length).toBeGreaterThan(30);
    expect(bySystem).toEqual(byAttribute);
  });

  it("у каждого цветового токена светлой темы есть тёмная пара", () => {
    const light = blockAfter("/* ——— Светлая тема ——— */");
    const dark = blockAfter(':root[data-theme="dark"] {');
    expect(names(dark)).toEqual(names(light));
  });

  it("стили компонентов берут цвета только из токенов", () => {
    const files = Object.entries(styles).filter(([path]) => !path.endsWith("/tokens.css"));
    expect(files.length).toBeGreaterThan(10);
    const offenders = files.flatMap(([path, css]) =>
      css
        .replace(/\/\*[\s\S]*?\*\//g, "")
        .split("\n")
        // Цвет в data-URI (%23…) тоже не переключится с темой.
        .filter((line) => /(#|%23)[0-9a-f]{3,8}\b|rgba?\(|hsla?\(/i.test(line))
        .map((line) => `${path}: ${line.trim()}`),
    );
    expect(offenders).toEqual([]);
  });
});
