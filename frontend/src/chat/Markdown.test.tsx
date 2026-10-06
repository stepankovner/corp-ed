import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { Markdown } from "./Markdown";

/** Ответ с маркерами: кнопка «номер карточки:номер выдержки». */
function answer(text: string, map?: (n: number) => number | undefined) {
  return render(
    <Markdown
      citationMap={map}
      renderCitation={(n, source) => <button type="button">{`${n}:${source}`}</button>}
    >
      {text}
    </Markdown>,
  );
}

const oneCard = () => 1;

describe("маркеры по карточкам (владелец 06.10)", () => {
  it("ссылки подряд на одну карточку — один маркер", () => {
    answer("Сюжет «За кулисами правды» [1][2][3][4][5][6][8][9].", oneCard);
    expect(screen.getAllByRole("button").map((b) => b.textContent)).toEqual(["1:1"]);
  });

  it("та же ссылка у соседних фраз абзаца — маркер только у последней", () => {
    const { container } = answer("Убийца — Волков [8]. Сообщник — Завьялов [8].", oneCard);
    expect(screen.getAllByRole("button").map((b) => b.textContent)).toEqual(["1:8"]);
    expect(container.textContent).toBe("Убийца — Волков. Сообщник — Завьялов 1:8.");
  });

  it("разные карточки между одинаковыми — все маркеры на месте", () => {
    answer("А [1]. Б [2]. В [1].", (n) => n);
    expect(screen.getAllByRole("button").map((b) => b.textContent)).toEqual(["1:1", "2:2", "1:1"]);
  });

  it("в группе — каждая карточка по разу, со своей первой выдержкой", () => {
    answer("Итог [3], [1], [2].", (n) => (n === 2 ? 2 : 1));
    expect(screen.getAllByRole("button").map((b) => b.textContent)).toEqual(["1:3", "2:2"]);
  });

  it("пункты списка сохраняют свои маркеры", () => {
    answer("- Первый [1]\n- Второй [1]", oneCard);
    expect(screen.getAllByRole("button")).toHaveLength(2);
  });

  it("маркер без источника остаётся текстом", () => {
    const { container } = answer("Пункт [7].", () => undefined);
    expect(screen.queryByRole("button")).not.toBeInTheDocument();
    expect(container.textContent).toBe("Пункт [7].");
  });

  it("без citationMap — как раньше: каждый маркер отдельно", () => {
    answer("Песочница [1][1].");
    expect(screen.getAllByRole("button").map((b) => b.textContent)).toEqual(["1:1", "1:1"]);
  });
});
