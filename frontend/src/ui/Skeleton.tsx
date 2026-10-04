import type { CSSProperties } from "react";

import styles from "./Skeleton.module.css";

/** Заглушка на месте загружаемого содержимого. Для скринридера — немая. */
export function Skeleton({
  width = "100%",
  height = 14,
  radius,
  className,
}: {
  width?: CSSProperties["width"];
  height?: CSSProperties["height"];
  radius?: CSSProperties["borderRadius"];
  className?: string;
}) {
  return (
    <span
      className={[styles.skeleton, className].filter(Boolean).join(" ")}
      style={{ width, height, borderRadius: radius }}
      aria-hidden
    />
  );
}

/** Строки-заглушки списка или таблицы; озвучивается одно «Загрузка». */
export function SkeletonList({ rows = 4, label = "Загрузка" }: { rows?: number; label?: string }) {
  return (
    <div className={styles.list} role="status" aria-label={label}>
      {Array.from({ length: rows }, (_, index) => (
        <div key={index} className={styles.row}>
          <Skeleton width={32} height={32} radius="var(--r-sm)" />
          <div className={styles.lines}>
            <Skeleton width={`${70 - ((index * 17) % 30)}%`} />
            <Skeleton width={`${40 - ((index * 11) % 20)}%`} height={10} />
          </div>
        </div>
      ))}
    </div>
  );
}
