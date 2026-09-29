import type { ReactNode } from "react";

import styles from "./Page.module.css";

export function Page({ children, wide = false }: { children: ReactNode; wide?: boolean }) {
  return (
    <div className={styles.page} style={wide ? { maxWidth: "none" } : undefined}>
      {children}
    </div>
  );
}

export function PageHeader({
  label,
  title,
  description,
  actions,
}: {
  label?: string;
  title: ReactNode;
  description?: ReactNode;
  actions?: ReactNode;
}) {
  return (
    <header className={styles.head}>
      <div>
        {label ? <p className={`mono ${styles.label}`}>{label}</p> : null}
        <h1 className={styles.title}>{title}</h1>
        {description ? <p className={styles.description}>{description}</p> : null}
      </div>
      {actions ? <div className={styles.actions}>{actions}</div> : null}
    </header>
  );
}

export function EmptyState({
  icon,
  title,
  children,
}: {
  icon?: ReactNode;
  title: ReactNode;
  children?: ReactNode;
}) {
  return (
    <div className={styles.empty}>
      {icon}
      <p className={styles.emptyTitle}>{title}</p>
      {children}
    </div>
  );
}
