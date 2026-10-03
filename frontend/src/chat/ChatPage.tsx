import { useCallback, useEffect, useRef, useState } from "react";

import { useMe } from "../auth/context";
import { useDocumentTitle } from "../lib/title";
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
  useDocumentTitle("Вопросы");
  const { turns, busy, ask, retry, vote } = useChat();
  const [opened, setOpened] = useState<Opened | null>(null);
  const lastQuestion = useRef<HTMLDivElement>(null);
  const trigger = useRef<HTMLElement | null>(null);
  const turnCount = turns.length;
  const lastState = turns.at(-1)?.state;

  // «Новый диалог» в боковой панели очистил переписку — панель источника тоже.
  if (opened && turnCount === 0) setOpened(null);

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

  return (
    <section className={styles.page} aria-labelledby="chat-title">
      <h1 className="visually-hidden" id="chat-title">
        Вопросы по документам
      </h1>
      <div className={styles.log} role="log" aria-label="Переписка" tabIndex={0}>
        <div className={styles.thread}>
          {turns.length === 0 ? (
            <div className={styles.welcome}>
              <span className={styles.welcomeMark}>
                <WindowMark size={40} />
              </span>
              <h2 className={styles.welcomeTitle}>Здравствуйте!</h2>
              <p className={styles.welcomeText}>
                Я отвечаю по документам {me.company_name} и к каждому ответу прикладываю источник.
                Если в документах ответа нет — скажу об этом прямо.
              </p>
            </div>
          ) : null}
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
                  <Spinner size={14} />
                  <span className={styles.searchingText}>Ищу в документах</span>
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
      </div>
      <p className="visually-hidden" aria-live="polite">
        {announce}
      </p>

      <Composer busy={busy} onAsk={(question) => void ask(question)} />

      {opened && openedSource ? (
        <SourcePanel source={openedSource} number={opened.index + 1} onClose={closeSource} />
      ) : null}
    </section>
  );
}
