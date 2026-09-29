import { ArrowUp } from "lucide-react";
import { useLayoutEffect, useRef, useState, type SubmitEvent, type KeyboardEvent } from "react";

import styles from "./Chat.module.css";

export const MAX_QUESTION = 1000;

export function Composer({ busy, onAsk }: { busy: boolean; onAsk: (question: string) => void }) {
  const [value, setValue] = useState("");
  const input = useRef<HTMLTextAreaElement>(null);
  const question = value.trim();
  const over = value.length > MAX_QUESTION;
  const disabled = busy || !question || over;

  // Поле растёт вместе с текстом до max-height из стилей.
  useLayoutEffect(() => {
    const node = input.current;
    if (!node) return;
    node.style.height = "auto";
    node.style.height = `${node.scrollHeight + 2}px`;
  }, [value]);

  function submit(event?: SubmitEvent) {
    event?.preventDefault();
    if (disabled) return;
    onAsk(question);
    setValue("");
    input.current?.focus();
  }

  function onKeyDown(event: KeyboardEvent<HTMLTextAreaElement>) {
    if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) {
      event.preventDefault();
      submit();
    }
  }

  return (
    <div className={styles.composer}>
      <form className={styles.form} onSubmit={submit}>
        <label className="visually-hidden" htmlFor="question">
          Ваш вопрос
        </label>
        <textarea
          ref={input}
          id="question"
          className={styles.input}
          rows={1}
          autoComplete="off"
          placeholder="Задайте вопрос по документам компании…"
          value={value}
          onChange={(e) => setValue(e.target.value)}
          onKeyDown={onKeyDown}
          aria-describedby="question-hint"
          autoFocus
        />
        <button
          className={styles.send}
          type="submit"
          disabled={disabled}
          aria-label="Отправить вопрос"
        >
          <ArrowUp size={18} strokeWidth={2} aria-hidden />
        </button>
      </form>
      <div className={styles.composerFoot} id="question-hint">
        <span className={styles.keysHint}>Enter — отправить, Shift+Enter — новая строка</span>
        {value.length > MAX_QUESTION * 0.8 ? (
          <span className={over ? styles.over : undefined}>
            {value.length} / {MAX_QUESTION}
          </span>
        ) : null}
      </div>
    </div>
  );
}
