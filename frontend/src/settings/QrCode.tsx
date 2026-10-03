import { useMemo } from "react";
import { encode } from "uqr";

import styles from "./Settings.module.css";

/**
 * QR-код из матрицы модулей — элементами SVG, без canvas, data:-адресов и
 * вставки разметки строкой. Поле вокруг — 4 модуля, как требует стандарт:
 * с меньшим камеры телефонов промахиваются.
 */
export function QrCode({ value, label }: { value: string; label: string }) {
  const { size, path } = useMemo(() => {
    const qr = encode(value, { ecc: "M", border: 4 });
    const parts: string[] = [];
    qr.data.forEach((row, y) => {
      row.forEach((dark, x) => {
        if (dark) parts.push(`M${x} ${y}h1v1h-1z`);
      });
    });
    return { size: qr.size, path: parts.join("") };
  }, [value]);

  return (
    <svg
      className={styles.qrImage}
      viewBox={`0 0 ${size} ${size}`}
      role="img"
      aria-label={label}
      shapeRendering="crispEdges"
    >
      <rect className={styles.qrBg} width={size} height={size} />
      <path className={styles.qrInk} d={path} />
    </svg>
  );
}
