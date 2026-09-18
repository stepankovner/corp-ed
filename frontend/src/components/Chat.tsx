import { useEffect, useRef, useState, type FormEvent } from "react";

import { errorMessage } from "../api/ApiError";
import { askFaq } from "../api/endpoints";
import type { FaqAnswer } from "../api/types";
import { useToken } from "../auth/AuthContext";
import { Button } from "./Button";
import { Notice } from "./Notice";
import { Spinner } from "./Spinner";
import styles from "./Chat.module.css";

const MAX_LENGTH = 1000;

const EXAMPLES = [
  "Как согласовать запуск кампании?",
  "Какие метрики смотреть в дашборде?",
];

interface Entry {
  id: number;
  question: string;
  answer: FaqAnswer;
}

/** Уникальные названия материалов в порядке появления. */
function sourceTitles(answer: FaqAnswer): string[] {
  const titles: string[] = [];
  for (const source of answer.sources) {
    if (!titles.includes(source.material_title)) {
      titles.push(source.material_title);
    }
  }
  return titles;
}

export function Chat() {
  const token = useToken();

  const [entries, setEntries] = useState<Entry[]>([]);
  const [question, setQuestion] = useState("");
  const [pending, setPending] = useState<string | null>(null);
  const [error, setError] = useState<unknown>(null);
  const bottomRef = useRef<HTMLDivElement>(null);

  // Новый ответ может оказаться ниже края окна — подкручиваем к нему,
  // иначе кажется, что ничего не произошло.
  useEffect(() => {
    if (entries.length > 0 || pending) {
      bottomRef.current?.scrollIntoView({ behavior: "smooth", block: "end" });
    }
  }, [entries.length, pending]);

  async function ask(asked: string) {
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

  function handleSubmit(event: FormEvent) {
    event.preventDefault();
    void ask(question.trim());
  }

  return (
    <div className={styles.chat}>
      {error ? <Notice tone="error">{errorMessage(error)}</Notice> : null}

      <div className={styles.feed}>
        {entries.length === 0 && !pending ? (
          <div className={styles.empty}>
            <span>
              Спросите о процессах, инструментах или регламентах отдела.
            </span>
            <div className={styles.examples}>
              {EXAMPLES.map((example) => (
                <button
                  key={example}
                  type="button"
                  className={styles.example}
                  onClick={() => void ask(example)}
                >
                  {example}
                </button>
              ))}
            </div>
          </div>
        ) : null}

        {entries.map((entry) => (
          <div key={entry.id} className="stack gap-16">
            <p className={styles.question}>{entry.question}</p>

            <div className={styles.reply}>
              <span className={styles.avatar} aria-hidden="true">
                k
              </span>

              {entry.answer.answer_given ? (
                <div className={styles.body}>
                  <p className={styles.text}>{entry.answer.content}</p>

                  {entry.answer.sources.length > 0 ? (
                    <div className={styles.sources}>
                      <span className="label">Источники</span>
                      {sourceTitles(entry.answer).map((title, index) => (
                        <div key={title} className={styles.source}>
                          <span className={styles.sourceNumber}>
                            {String(index + 1).padStart(2, "0")}
                          </span>
                          <span>{title}</span>
                        </div>
                      ))}
                    </div>
                  ) : null}
                </div>
              ) : (
                <div className={`${styles.body} ${styles.refusal}`}>
                  <span className="label">Нет в материалах отдела</span>
                  <p className={styles.refusalText}>{entry.answer.content}</p>
                  <span className={styles.footnote}>
                    Бот не придумывает ответ, если его нет в документах отдела.
                  </span>
                </div>
              )}
            </div>
          </div>
        ))}

        {pending ? (
          <div className="stack gap-16">
            <p className={styles.question}>{pending}</p>
            <div className={styles.thinking}>
              <Spinner />
              <span>Ищем ответ в материалах отдела…</span>
            </div>
          </div>
        ) : null}

        <div ref={bottomRef} />
      </div>

      <form className={styles.composer} onSubmit={handleSubmit}>
        <textarea
          className={styles.input}
          value={question}
          onChange={(event) => setQuestion(event.target.value)}
          placeholder="Спросите о процессах, инструментах или регламентах отдела"
          aria-label="Вопрос"
          maxLength={MAX_LENGTH}
          rows={2}
        />
        <div className={styles.composerFoot}>
          <span className="mono">
            {question.length} / {MAX_LENGTH}
          </span>
          <Button type="submit" loading={pending !== null}>
            {pending ? "Ищем" : "Отправить"}
          </Button>
        </div>
      </form>
    </div>
  );
}
