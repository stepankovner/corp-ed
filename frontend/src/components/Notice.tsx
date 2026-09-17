import type { ReactNode } from "react";

import styles from "./Notice.module.css";

interface Props {
  tone?: "error" | "info" | "success";
  children: ReactNode;
}

/** Сообщение пользователю: ошибка, подсказка, подтверждение. */
export function Notice({ tone = "info", children }: Props) {
  return (
    <div className={`${styles.notice} ${styles[tone]}`} role="status">
      {children}
    </div>
  );
}
