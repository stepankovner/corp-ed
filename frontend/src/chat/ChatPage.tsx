import { RotateCcw } from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";

import { useMe } from "../auth/context";
import { Button } from "../ui/Button";
import { WindowMark } from "../ui/Logo";
import { Spinner } from "../ui/Spinner";
import { AnswerView, FailedView } from "./AnswerView";
import styles from "./Chat.module.css";
import { Composer } from "./Composer";
import { SourcePanel } from "./SourcePanel";
import { useChat } from "./store";

interface Opened {
  turnId: string;
  index: number;
}

export function ChatPage() {
  const me = useMe();
  const { turns, busy, ask, retry, vote, reset } = useChat();
  const [opened, setOpened] = useState<Opened | null>(null);
  const log = useRef<HTMLDivElement>(null);
  const lastQuestion = useRef<HTMLDivElement>(null);
  const trigger = useRef<HTMLElement | null>(null);
  const turnCount = turns.length;
  const lastState = turns.at(-1)?.state;

  // Новый вопрос — прокручиваем к нему; ответ появится ниже.
  useEffect(() => {
    lastQuestion.current?.scrollIntoView({ block: "start", behavior: "smooth" });
  }, [turnCount]);

  const announce =
    lastState === "done"
      ? "Ответ готов."
      : lastState === "error"
        ? "Ошибка, ответа нет."
        : lastState === "pending"
          ? "Ищу в документах…"
          : "";

  const openSource = useCallback((turnId: string, index: number) => {
    trigger.current = document.activeElement as HTMLElement | null;
    setOpened({ turnId, index });
  }, []);

  const closeSource = useCallback(() => {
    setOpened(null);
    trigger.current?.focus();
  }, []);

  const openedTurn = opened ? turns.find((turn) => turn.id === opened.turnId) : undefined;
  const openedSource =
    opened && openedTurn?.state === "done" ? openedTurn.answer.sources[opened.index] : undefined;

  function startOver() {
    setOpened(null);
    reset();
  }

  return (
    <div className={styles.page}>
      <section className={styles.window} aria-label="Вопросы по документам">
        <div className={styles.bar}>
          <span className={styles.title}>
            <WindowMark />
            <span className={styles.company}>{me.company_name}</span>
          </span>
          <Button
            variant="ghost"
            size="xs"
            onClick={startOver}
            disabled={busy || turns.length === 0}
          >
            <RotateCcw size={14} aria-hidden />
            Новый диалог
          </Button>
        </div>

        <div className={styles.body}>
          <div className={styles.log} ref={log} role="log" aria-label="Переписка" tabIndex={0}>
            <div className={`${styles.msg} ${styles.bot} ${styles.intro}`}>
              <p>
                Здравствуйте! Я отвечаю по документам {me.company_name} и к каждому ответу
                прикладываю источник. Если в документах ответа нет — скажу об этом прямо.
              </p>
            </div>
            {turns.map((turn, index) => (
              <div key={turn.id} style={{ display: "contents" }}>
                <div
                  className={`${styles.msg} ${styles.user}`}
                  ref={index === turns.length - 1 ? lastQuestion : undefined}
                >
                  {turn.question}
                </div>
                {turn.state === "pending" ? (
                  <div className={`${styles.msg} ${styles.bot} ${styles.searching}`}>
                    Ищу в документах <Spinner size={12} />
                  </div>
                ) : turn.state === "done" ? (
                  <AnswerView
                    turn={turn}
                    openSource={opened}
                    onOpenSource={(i) => openSource(turn.id, i)}
                    onVote={(value) => void vote(turn.id, value)}
                  />
                ) : (
                  <FailedView
                    turn={turn}
                    isAdmin={me.role === "admin"}
                    onRetry={() => void retry(turn.id)}
                  />
                )}
              </div>
            ))}
          </div>
          <p className="visually-hidden" aria-live="polite">
            {announce}
          </p>

          <Composer busy={busy} onAsk={(question) => void ask(question)} />

          {opened && openedSource ? (
            <SourcePanel source={openedSource} number={opened.index + 1} onClose={closeSource} />
          ) : null}
        </div>
      </section>
    </div>
  );
}
