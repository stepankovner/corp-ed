import { describe, expect, it } from "vitest";

import { formatCalendarDate } from "./format";

describe("formatCalendarDate", () => {
  it("берёт дату из строки сервера, а не из пояса браузера", () => {
    // Полночь по Москве — 21:00 UTC накануне: прежний formatDate показывал
    // западнее Москвы предыдущий день.
    expect(formatCalendarDate("2026-10-01T00:00:00+03:00")).toBe("1 октября 2026 г.");
  });

  it("исключающая граница — последний день периода", () => {
    expect(formatCalendarDate("2026-11-01T00:00:00+03:00", -1)).toBe("31 октября 2026 г.");
    expect(formatCalendarDate("2027-01-01T00:00:00+03:00", -1)).toBe("31 декабря 2026 г.");
  });

  it("пусто и мусор — прочерк", () => {
    expect(formatCalendarDate(null)).toBe("—");
    expect(formatCalendarDate("вчера")).toBe("—");
  });
});
