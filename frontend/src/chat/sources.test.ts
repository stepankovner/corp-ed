import { describe, expect, it } from "vitest";

import { answer } from "../test/fixtures";
import { fragmentText, groupSources, sourceGroupOf, sourceSection } from "./sources";

const [withPath, withoutPath] = answer().sources;

describe("fragmentText", () => {
  it("drops the breadcrumb line the backend adds for the model", () => {
    expect(fragmentText(withPath!)).toBe(
      "Суточные при командировках по России — 700 рублей в сутки.",
    );
  });

  it("drops a title-only breadcrumb", () => {
    expect(fragmentText(withoutPath!)).toBe("За рубеж — 2500 рублей.");
  });

  it("keeps text whose first line is not the breadcrumb", () => {
    const source = { ...withPath!, content: "Первая строка документа\nВторая" };
    expect(fragmentText(source)).toBe("Первая строка документа\nВторая");
  });
});

describe("sourceSection", () => {
  it("joins the heading path", () => {
    expect(sourceSection(withPath!)).toBe("Положение о командировках › 2. Суточные");
  });
});

describe("groupSources", () => {
  const section = { title: "Положение.docx", heading_path: ["3. Отпуск"] };
  const sources = [
    { ...section, kind: "document", material_id: "m-1", position: 4 },
    { title: "Приказ", heading_path: [], kind: "document", material_id: "m-2", position: 0 },
    { ...section, kind: "document", material_id: "m-1", position: 5 },
    { ...section, kind: "document", material_id: "m-1", position: 9, heading_path: ["4. Бол"] },
    { ...section, kind: "attachment", material_id: null, attachment_id: "f-1", position: 0 },
  ];

  it("puts fragments of one section on one card, numbered as in the answer", () => {
    const groups = groupSources(sources);
    expect(groups.map((group) => group.numbers)).toEqual([[1, 3], [2], [4], [5]]);
    expect(groups[0]?.index).toBe(0);
    expect(groups[0]?.sources.map((source) => source.position)).toEqual([4, 5]);
  });

  it("finds the card of a cited source", () => {
    expect(sourceGroupOf(sources, 2)?.numbers).toEqual([1, 3]);
    expect(sourceGroupOf(sources, 9)).toBeUndefined();
  });

  it("groups sandbox sources without ids by title and section", () => {
    const demo = [section, section, { ...section, title: "Другой" }];
    expect(groupSources(demo).map((group) => group.numbers)).toEqual([[1, 2], [3]]);
  });
});
