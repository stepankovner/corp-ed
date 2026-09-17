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
        <p className="eyebrow">Вопросы по компании</p>
        <h1 className="page-heading">
          Спросите то,{" "}
          <span className="display-muted">что неудобно спрашивать людей.</span>
        </h1>
        <p className="muted">
          Бот отвечает только по материалам компании и показывает, откуда
          взял ответ.
        </p>
      </header>

      {error ? <Notice tone="error">{errorMessage(error)}</Notice> : null}

      <div className={styles.feed}>
        {entries.length === 0 && !pending ? (
          <div className={styles.empty}>
            Задайте первый вопрос.
            <div className={styles.examples}>
              <span>— За сколько дней подавать заявление на отпуск?</span>
              <span>— Как получить доступ к рекламному кабинету?</span>
            </div>
          </div>
        ) : null}

        {entries.map((entry) => (
          <div key={entry.id} className="stack gap-16">
            <p className={styles.question}>{entry.question}</p>

            <article
              className={`${styles.answer} ${
                entry.answer.answer_given ? "" : styles.refusal
              }`}
            >
              <p
                className={`${styles.label} ${
                  entry.answer.answer_given ? "" : styles.refusalLabel
                }`}
              >
                {entry.answer.answer_given
                  ? "Ответ по материалам компании"
                  : "В материалах компании нет ответа"}
              </p>

              <p className={styles.text}>{entry.answer.content}</p>

              {entry.answer.sources.length > 0 ? (
                <details className={styles.sources}>
                  <summary className={styles.sourcesSummary}>
                    Источники: {entry.answer.sources.length}
                  </summary>
                  {entry.answer.sources.map((source) => (
                    <div
                      key={`${source.material_id}-${source.position}`}
                      className={styles.source}
                    >
                      <span className={styles.sourceMeta}>
                        Материал {shortId(source.material_id)} · фрагмент{" "}
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
          <div className="stack gap-16">
            <p className={styles.question}>{pending}</p>
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
