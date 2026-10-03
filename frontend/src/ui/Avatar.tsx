import type { ReactNode } from "react";

import { tileIndex } from "../lib/initials";
import styles from "./Avatar.module.css";

type Size = "sm" | "md" | "lg";

/**
 * Плитка с инициалами: круглая — человек, квадратная — компания. Цвет
 * («colorful») выводится из названия и не меняется между входами.
 */
export function Avatar({
  initials,
  name,
  shape = "circle",
  size = "md",
  colorful = false,
  className,
}: {
  /** Инициалы или значок (компании ещё нет). */
  initials: ReactNode;
  /** Полное имя или название: от него — цвет плитки. */
  name: string;
  shape?: "circle" | "square";
  size?: Size;
  colorful?: boolean;
  className?: string;
}) {
  const tone = colorful ? `var(--tile-${tileIndex(name)})` : undefined;
  return (
    <span
      className={[
        styles.avatar,
        styles[shape],
        styles[size],
        colorful ? styles.tile : "",
        className,
      ]
        .filter(Boolean)
        .join(" ")}
      style={tone ? { background: tone } : undefined}
      aria-hidden
    >
      {initials}
    </span>
  );
}
