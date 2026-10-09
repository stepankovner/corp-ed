import { describe, expect, it } from "vitest";

import { aboutCredits, averageQuestionNote, credits, formatKopecks } from "./credits";

describe("кредиты", () => {
  it("склоняет слово", () => {
    expect(credits(1)).toBe("1 кредит");
    expect(credits(3)).toBe("3 кредита");
    expect(credits(2000)).toBe("2\u00a0000 кредитов");
  });

  it("«около N кредитов» — в родительном падеже", () => {
    expect(aboutCredits(1)).toBe("около 1 кредита");
    expect(aboutCredits(2)).toBe("около 2 кредитов");
    expect(aboutCredits(21)).toBe("около 21 кредита");
    expect(aboutCredits(1.5)).toBe("около 1,5 кредита");
    expect(averageQuestionNote(1)).toBe(
      "В среднем один вопрос — около 1 кредита; длинные вопросы и ответы списывают больше.",
    );
  });

  it("сумма из копеек", () => {
    expect(formatKopecks(549_000)).toBe("5\u00a0490\u00a0₽");
    expect(formatKopecks(149_050)).toBe("1\u00a0490,50\u00a0₽");
  });
});
