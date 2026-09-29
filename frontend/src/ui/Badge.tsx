import type { ReactNode } from "react";

import styles from "./Badge.module.css";

export type Tone = "warn" | "muted" | "ok" | "error" | "accent";

export function Badge({
  tone = "muted",
  wrap = false,
  children,
}: {
  tone?: Tone;
  wrap?: boolean;
  children: ReactNode;
}) {
  return (
    <span className={[styles.badge, styles[tone], wrap ? styles.wrap : ""].join(" ")}>
      {children}
    </span>
  );
}
