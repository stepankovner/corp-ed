import type { ReactNode } from "react";

import styles from "./Notice.module.css";

interface Props {
  tone?: "error" | "info";
  children: ReactNode;
}

/** Сообщение пользователю. Ошибку помечает знак, а не красная заливка. */
export function Notice({ tone = "info", children }: Props) {
  return (
    <div className={`${styles.notice} ${styles[tone]}`} role="status">
      {tone === "error" ? <span className={styles.mark}>!</span> : null}
      <span>{children}</span>
    </div>
  );
}
