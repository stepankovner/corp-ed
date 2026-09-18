import { Chat } from "../components/Chat";
import styles from "./ChatPage.module.css";

/** Тот же чат, что видит стажёр: руководителю он нужен, чтобы проверить,
 *  как бот отвечает по загруженным материалам. */
export function ChatPage() {
  return (
    <div className={styles.page}>
      <header className={styles.head}>
        <h1 className="h1">Чат по материалам</h1>
        <p className="lead">
          Отвечает только по документам отдела. Если ответа в них нет — так и
          скажет.
        </p>
      </header>

      <Chat />
    </div>
  );
}
