import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { SOURCES } from "../test/chat";
import { UiProvider } from "../ui/UiProvider";
import { SourcePanel } from "./SourcePanel";

const CONTENT = "Суточные по России — 700 рублей [1]. За рубеж — 2500 рублей [2].";

/** Ответ со ссылкой [1]: кнопка открывает панель, панель закрывается сама. */
function Answer() {
  const [open, setOpen] = useState(false);
  return (
    <UiProvider>
      <button type="button" onClick={() => setOpen(true)}>
        Источник 1
      </button>
      <button type="button">Дальше по странице</button>
      {open ? (
        <SourcePanel sources={SOURCES} content={CONTENT} index={0} onClose={() => setOpen(false)} />
      ) : null}
    </UiProvider>
  );
}

/** Телефон: панель — лист снизу поверх затемнения (Chat.module.css, ≤760px). */
function onPhone() {
  window.matchMedia = vi.fn((query: string) => ({
    matches: query.includes("max-width"),
    media: query,
    addEventListener: () => undefined,
    removeEventListener: () => undefined,
  })) as unknown as typeof window.matchMedia;
}

afterEach(() => {
  // @ts-expect-error -- в jsdom matchMedia нет, возвращаем как было.
  delete window.matchMedia;
});

describe("панель источника", () => {
  it("фокус — в панель, Esc закрывает и возвращает фокус к ссылке", async () => {
    const user = userEvent.setup();
    render(<Answer />);
    const trigger = screen.getByRole("button", { name: "Источник 1" });

    await user.click(trigger);
    const panel = screen.getByRole("dialog", { name: "Положение о командировках.docx" });
    expect(panel).toHaveFocus();

    await user.keyboard("{Escape}");
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(trigger).toHaveFocus();
  });

  it("кнопка «Закрыть» тоже возвращает фокус к ссылке", async () => {
    const user = userEvent.setup();
    render(<Answer />);
    const trigger = screen.getByRole("button", { name: "Источник 1" });

    await user.click(trigger);
    await user.click(screen.getByRole("button", { name: "Закрыть источник" }));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(trigger).toHaveFocus();
  });

  it("на широком экране — боковая панель: не модальная, Tab уходит дальше", async () => {
    const user = userEvent.setup();
    render(<Answer />);
    await user.click(screen.getByRole("button", { name: "Источник 1" }));
    const panel = screen.getByRole("dialog");
    expect(panel).toHaveAttribute("aria-modal", "false");

    const link = screen.getByRole("link", { name: /Открыть документ/ });
    link.focus();
    await user.tab();
    expect(panel).not.toContainElement(document.activeElement as HTMLElement);
  });

  it("на телефоне — модальный лист: Tab не выходит из панели", async () => {
    onPhone();
    const user = userEvent.setup();
    render(<Answer />);
    await user.click(screen.getByRole("button", { name: "Источник 1" }));
    const panel = screen.getByRole("dialog");
    expect(panel).toHaveAttribute("aria-modal", "true");

    const close = screen.getByRole("button", { name: "Закрыть источник" });
    const link = screen.getByRole("link", { name: /Открыть документ/ });
    link.focus();
    await user.tab();
    expect(close).toHaveFocus();
    await user.tab({ shift: true });
    expect(link).toHaveFocus();
  });
});
