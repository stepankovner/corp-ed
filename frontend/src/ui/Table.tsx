import type { ReactNode } from "react";

import styles from "./Table.module.css";

export function Table({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className={styles.wrap}>
      <table className={styles.table} aria-label={label}>
        {children}
      </table>
    </div>
  );
}
