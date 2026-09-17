import { useEffect, useRef, useState, type FormEvent } from "react";

import { errorMessage } from "../api/ApiError";
import { askFaq } from "../api/endpoints";
import type { FaqAnswer } from "../api/types";
import { useToken } from "../auth/AuthContext";
import { Button } from "../components/Button";
import { Notice } from "../components/Notice";
import { Spinner } from "../components/Spinner";
import styles from "./ChatPage.module.css";

interface Entry {
  id: number;
  question: string;
  answer: FaqAnswer;
}

/** Короткий вид идентификатора материала: полный UUID в ленте не читается. */
function shortId(id: string): string {
  return id.slice(0, 8);
}

export function ChatPage() {
  const token = useToken();

  const [entries, setEntries] = useState<Entry[]>([]);
  const [question, setQuestion] = useState("");
  const [pending, setPending] = useState<string | null>(null);
  const [error, setError] = useState<unknown>(null);
  const bottomRef = useRef<HTMLDivElement>(null);

  // Новый ответ может оказаться ниже края окна — подкручиваем к нему,
  // иначе кажется, что ничего не произошло.
  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [entries.length, pending]);

  async function handleAsk(event: FormEvent) {
    event.preventDefault();

    const asked = question.trim();
    if (!asked || pending) {
      return;
    }

    setError(null);
    setPending(asked);
    setQuestion("");

    try {
      const answer = await askFaq(token, asked);
      setEntries((previous) => [
        ...previous,
        { id: previous.length, question: asked, answer },
      ]);
    } catch (caught) {
      setError(caught);
      // Вопрос возвращается в поле: переписывать его заново обидно.
      setQuestion(asked);
    } finally {
      setPending(null);
    }
  }

  return (
    <div className={styles.page}>
      <header className={styles.head}>
        <h1 className="title">Вопросы</h1>
        <p className="subtitle">
          Ответы собираются по документам компании, без домыслов.
        </p>
      </header>

      {error ? <Notice tone="error">{errorMessage(error)}</Notice> : null}

      <div className={styles.feed}>
        {entries.length === 0 && !pending ? (
          <div className={styles.empty}>
            Здесь пока пусто.
            <div className={styles.examples}>
              <span>За сколько дней подавать заявление на отпуск?</span>
              <span>Как получить доступ к рекламному кабинету?</span>
            </div>
          </div>
        ) : null}

        {entries.map((entry) => (
          <div key={entry.id} className={styles.exchange}>
            <div className={styles.question}>
              <span className="meta">вопрос</span>
              <p className={styles.questionText}>{entry.question}</p>
            </div>

            <article
              className={`${styles.answer} ${
                entry.answer.answer_given ? "" : styles.refusal
              }`}
            >
              <span className="meta">
                {entry.answer.answer_given
                  ? "ответ по материалам компании"
                  : "ответа в материалах компании нет"}
              </span>

              <p
                className={`${styles.text} ${
                  entry.answer.answer_given ? "" : styles.refusalText
                }`}
              >
                {entry.answer.content}
              </p>

              {entry.answer.sources.length > 0 ? (
                <details className={styles.sources}>
                  <summary className={styles.sourcesSummary}>
                    Источники · {entry.answer.sources.length}
                  </summary>
                  {entry.answer.sources.map((source) => (
                    <div
                      key={`${source.material_id}-${source.position}`}
                      className={styles.source}
                    >
                      <span className={`meta ${styles.sourceMeta}`}>
                        материал {shortId(source.material_id)} · фрагмент{" "}
                        {source.position + 1}
                      </span>
                      {source.content}
                    </div>
                  ))}
                </details>
              ) : null}
            </article>
          </div>
        ))}

        {pending ? (
          <div className={styles.exchange}>
            <div className={styles.question}>
              <span className="meta">вопрос</span>
              <p className={styles.questionText}>{pending}</p>
            </div>
            <div className={styles.thinking}>
              <Spinner />
              <span>Ищем ответ в материалах…</span>
            </div>
          </div>
        ) : null}

        <div ref={bottomRef} />
      </div>

      <form className={styles.form} onSubmit={handleAsk}>
        <input
          className={styles.input}
          value={question}
          onChange={(event) => setQuestion(event.target.value)}
          placeholder="Ваш вопрос"
          aria-label="Вопрос"
          maxLength={1000}
        />
        <Button type="submit" loading={pending !== null}>
          {pending ? "Ищем" : "Спросить"}
        </Button>
      </form>
    </div>
  );
}
