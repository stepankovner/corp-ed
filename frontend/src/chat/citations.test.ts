import { describe, expect, it } from "vitest";

import { citedNumbers, splitCitations, stripGeneralPrefix } from "./citations";

describe("splitCitations", () => {
  it("splits single and grouped markers", () => {
    expect(splitCitations("Суточные — 700 ₽ [1]. См. также [2, 3].")).toEqual([
      "Суточные — 700 ₽ ",
      1,
      ". См. также ",
      2,
      3,
      ".",
    ]);
  });

  it("expands ranges", () => {
    expect(splitCitations("См. [1–3] и [5-6].")).toEqual(["См. ", 1, 2, 3, " и ", 5, 6, "."]);
  });

  it("does not treat clause numbers as markers", () => {
    expect(splitCitations("по пункту [2.2]")).toEqual(["по пункту [2.2]"]);
  });

  it("leaves text without markers intact", () => {
    expect(splitCitations("Просто текст [а]")).toEqual(["Просто текст [а]"]);
  });

  it("collects cited numbers", () => {
    expect([...citedNumbers("[1] и [3][1]")]).toEqual([1, 3]);
  });
});

describe("stripGeneralPrefix", () => {
  it("removes the service first line of a general answer", () => {
    const content =
      "В документах компании ответа нет. Ниже — общая информация, не из документов компании:\nСтолица — Канберра.";
    expect(stripGeneralPrefix(content)).toBe("Столица — Канберра.");
  });

  it("keeps answers without the prefix", () => {
    expect(stripGeneralPrefix("Ответ.")).toBe("Ответ.");
  });
});
