import { Link } from "react-router";

import { useDocumentTitle } from "../lib/title";
import { buttonClass } from "../ui/buttonClass";
import { WindowMark } from "../ui/Logo";
import styles from "./NotFoundPage.module.css";

export function NotFoundPage() {
  useDocumentTitle("Страница не найдена");
  return (
    <div className={styles.screen}>
      <span className={styles.mark}>
        <WindowMark size={32} />
      </span>
      <p className={`mono ${styles.code}`}>ошибка 404</p>
      <h1 className={styles.title}>Такой страницы нет</h1>
      <p className={styles.text}>Возможно, ссылка устарела или в адресе опечатка.</p>
      <Link className={buttonClass("dark", "sm")} to="/">
        К вопросам
      </Link>
    </div>
  );
}
