import type { ReactNode } from "react";

import styles from "./Notice.module.css";

interface Props {
  tone?: "error" | "info" | "success";
  children: ReactNode;
}

const LABELS = {
  error: "Ошибка",
  info: null,
  success: null,
} as const;

/** Сообщение пользователю. Тон различается подписью, а не краской. */
export function Notice({ tone = "info", children }: Props) {
  const label = LABELS[tone];

  return (
    <div className={`${styles.notice} ${styles[tone]}`} role="status">
      {label ? <span className={styles.label}>{label}</span> : null}
      <span>{children}</span>
    </div>
  );
}
