import { describe, expect, it } from "vitest";

import { answer } from "../test/fixtures";
import { fragmentText, sourceSection } from "./sources";

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
