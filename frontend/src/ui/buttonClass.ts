import styles from "./Button.module.css";

export type ButtonVariant = "dark" | "accent" | "ghost" | "danger" | "link";
export type ButtonSize = "md" | "sm" | "xs";

export function buttonClass(
  variant: ButtonVariant = "dark",
  size: ButtonSize = "md",
  block = false,
): string {
  return [styles.btn, styles[variant], size === "md" ? "" : styles[size], block ? styles.block : ""]
    .filter(Boolean)
    .join(" ");
}
