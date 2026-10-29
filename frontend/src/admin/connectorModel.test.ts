import { describe, expect, it } from "vitest";

import { defaultName, type Kind } from "./connectorModel";

const kind = (title: string) => ({ title }) as Kind;

describe("название нового подключения", () => {
  it("система без пояснения в скобках", () => {
    expect(defaultName(kind("Битрикс24 (облако и коробка)"))).toBe("Битрикс24");
    expect(defaultName(kind("Yonote (база знаний)"))).toBe("Yonote");
    expect(defaultName(kind("Kaiten"))).toBe("Kaiten");
    expect(defaultName(kind("(без названия)"))).toBe("(без названия)");
  });
});
