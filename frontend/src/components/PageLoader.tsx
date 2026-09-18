import { Spinner } from "./Spinner";
import styles from "./PageLoader.module.css";

/** Заглушка на время загрузки: пустого экрана без объяснения быть не должно. */
export function PageLoader({ text = "Загружаем" }: { text?: string }) {
  return (
    <div className={styles.loader}>
      <Spinner />
      <span>{text}…</span>
    </div>
  );
}
