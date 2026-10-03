import { useLayoutEffect, useRef, type ReactNode } from "react";

import styles from "./Table.module.css";

/**
 * Таблица. На телефоне строки становятся карточками, а у ячеек появляется
 * подпись из заголовка колонки (data-label — его читает CSS). Подписи
 * расставляются после каждой отрисовки: строки приходят из страниц как есть.
 */
export function Table({
  label,
  rowTitle = true,
  children,
}: {
  label: string;
  /** Первая колонка — название строки (документ, сотрудник): на телефоне без подписи. */
  rowTitle?: boolean;
  children: ReactNode;
}) {
  const table = useRef<HTMLTableElement>(null);

  useLayoutEffect(() => {
    const node = table.current;
    if (!node) return;
    const heads = Array.from(node.tHead?.rows[0]?.cells ?? [], (cell) =>
      cell.querySelector(".visually-hidden") ? "" : cell.textContent.trim(),
    );
    for (const body of Array.from(node.tBodies)) {
      for (const row of Array.from(body.rows)) {
        Array.from(row.cells).forEach((cell, index) => {
          const text = heads[index] ?? "";
          if (cell.dataset.label !== text) cell.dataset.label = text;
        });
      }
    }
  });

  return (
    <div className={styles.wrap}>
      <table
        ref={table}
        className={`${styles.table} ${rowTitle ? styles.rowTitle : ""}`}
        aria-label={label}
      >
        {children}
      </table>
    </div>
  );
}
