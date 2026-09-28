import styles from "./Spinner.module.css";

export function Spinner({ size = 18, label }: { size?: number; label?: string }) {
  return (
    <span
      className={styles.spinner}
      style={{ width: size, height: size }}
      role={label ? "status" : undefined}
      aria-label={label}
      aria-hidden={label ? undefined : true}
    />
  );
}

export function PageSpinner() {
  return (
    <div className={styles.page}>
      <Spinner size={28} label="Загрузка" />
    </div>
  );
}
