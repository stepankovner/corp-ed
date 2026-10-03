import type { ComponentProps } from "react";

import styles from "./Card.module.css";

type Props = ComponentProps<"div"> & {
  /** Внутренний отступ: обычный или плотный — для списков карточек. */
  padding?: "md" | "sm";
  /** Кремовая подложка вместо белой карточки с рамкой. */
  tone?: "plain" | "muted";
};

/** Карточка: рамка, скругление и отступы — как у карточек лендинга. */
export function Card({ padding = "md", tone = "plain", className, ...rest }: Props) {
  return (
    <div
      className={[styles.card, styles[padding], styles[tone], className].filter(Boolean).join(" ")}
      {...rest}
    />
  );
}
