import type { ComponentProps, ReactNode } from "react";

import styles from "./IconButton.module.css";
import { Tooltip } from "./Tooltip";

interface Props extends ComponentProps<"button"> {
  label: string;
  size?: "md" | "sm";
  active?: boolean;
  /** Подпись-подсказка при наведении; false — без неё (подпись уже видна рядом). */
  tooltip?: boolean;
  tooltipSide?: "top" | "right" | "bottom" | "left";
  children: ReactNode;
}

export function IconButton({
  label,
  size = "md",
  active = false,
  tooltip = true,
  tooltipSide,
  className,
  children,
  ...rest
}: Props) {
  return (
    <Tooltip content={label} side={tooltipSide} disabled={!tooltip}>
      <button
        type="button"
        aria-label={label}
        className={[
          styles.iconBtn,
          size === "sm" ? styles.sm : "",
          active ? styles.active : "",
          className,
        ]
          .filter(Boolean)
          .join(" ")}
        {...rest}
      >
        {children}
      </button>
    </Tooltip>
  );
}
