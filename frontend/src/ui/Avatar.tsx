import { useState, type ReactNode } from "react";

import { tileIndex } from "../lib/initials";
import styles from "./Avatar.module.css";

type Size = "sm" | "md" | "lg" | "xl";

/**
 * Плитка с фото или инициалами: круглая — человек, квадратная — компания.
 * Цвет («colorful») выводится из названия и не меняется между входами.
 * Фото не загрузилось (ссылка истекла, нет сети) — снова инициалы.
 */
export function Avatar({
  initials,
  name,
  src,
  shape = "circle",
  size = "md",
  colorful = false,
  className,
}: {
  /** Инициалы или значок (компании ещё нет). */
  initials: ReactNode;
  /** Полное имя или название: от него — цвет плитки. */
  name: string;
  /** Фото профиля (подписанная ссылка API). */
  src?: string | null;
  shape?: "circle" | "square";
  size?: Size;
  colorful?: boolean;
  className?: string;
}) {
  const [failed, setFailed] = useState<string | null>(null);
  const photo = src && failed !== src ? src : null;
  const tone = colorful && !photo ? `var(--tile-${tileIndex(name)})` : undefined;
  return (
    <span
      className={[
        styles.avatar,
        styles[shape],
        styles[size],
        colorful && !photo ? styles.tile : "",
        photo ? styles.photo : "",
        className,
      ]
        .filter(Boolean)
        .join(" ")}
      style={tone ? { background: tone } : undefined}
      aria-hidden
    >
      {photo ? (
        <img src={photo} alt="" loading="lazy" decoding="async" onError={() => setFailed(photo)} />
      ) : (
        initials
      )}
    </span>
  );
}
