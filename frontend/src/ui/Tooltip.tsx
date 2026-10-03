import * as RadixTooltip from "@radix-ui/react-tooltip";
import type { ReactElement, ReactNode } from "react";

import styles from "./Tooltip.module.css";

/** Общая задержка подсказок: между соседними кнопками вторая открывается сразу. */
export function TooltipProvider({ children }: { children: ReactNode }) {
  return (
    <RadixTooltip.Provider delayDuration={400} skipDelayDuration={300}>
      {children}
    </RadixTooltip.Provider>
  );
}

/**
 * Подсказка при наведении и фокусе с клавиатуры. Доступное имя элемента не
 * меняет: подпись кнопки-иконки по-прежнему в её aria-label.
 */
export function Tooltip({
  content,
  side = "top",
  disabled = false,
  children,
}: {
  content: ReactNode;
  side?: "top" | "right" | "bottom" | "left";
  disabled?: boolean;
  /** Один элемент, принимающий ref и обработчики (кнопка, ссылка). */
  children: ReactElement;
}) {
  if (disabled) return children;
  return (
    <RadixTooltip.Root>
      <RadixTooltip.Trigger asChild>{children}</RadixTooltip.Trigger>
      <RadixTooltip.Portal>
        <RadixTooltip.Content className={styles.content} side={side} sideOffset={6}>
          {content}
        </RadixTooltip.Content>
      </RadixTooltip.Portal>
    </RadixTooltip.Root>
  );
}
