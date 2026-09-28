import { CircleAlert, CircleCheck, Info, TriangleAlert } from "lucide-react";
import type { ReactNode } from "react";

import styles from "./Notice.module.css";

type Kind = "info" | "warn" | "error" | "ok";

const ICONS = { info: Info, warn: TriangleAlert, error: CircleAlert, ok: CircleCheck };

export function Notice({
  kind = "info",
  title,
  children,
}: {
  kind?: Kind;
  title?: ReactNode;
  children?: ReactNode;
}) {
  const Icon = ICONS[kind];
  return (
    <div
      className={`${styles.notice} ${styles[kind]}`}
      role={kind === "error" ? "alert" : "status"}
    >
      <Icon size={18} aria-hidden />
      <div className={styles.body}>
        {title ? <p className={styles.title}>{title}</p> : null}
        {children ? <div>{children}</div> : null}
      </div>
    </div>
  );
}
