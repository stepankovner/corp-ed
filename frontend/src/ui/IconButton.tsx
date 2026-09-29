import type { ButtonHTMLAttributes, ReactNode } from "react";

import styles from "./IconButton.module.css";

interface Props extends ButtonHTMLAttributes<HTMLButtonElement> {
  label: string;
  size?: "md" | "sm";
  active?: boolean;
  children: ReactNode;
}

export function IconButton({
  label,
  size = "md",
  active = false,
  className,
  children,
  ...rest
}: Props) {
  return (
    <button
      type="button"
      aria-label={label}
      title={label}
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
  );
}
