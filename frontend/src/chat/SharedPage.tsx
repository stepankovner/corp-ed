import { useQuery } from "@tanstack/react-query";
import { Link2Off } from "lucide-react";
import { useCallback, useRef, useState } from "react";
import { Link, useParams } from "react-router";

import { api, unwrap } from "../api/client";
import { formatDateTime } from "../lib/format";
import { useDocumentTitle } from "../lib/title";
import { buttonClass } from "../ui/buttonClass";
import { EmptyState } from "../ui/Page";
import { PageSpinner } from "../ui/Spinner";
import { AnswerBody, UserMessage, type OpenSource } from "./AnswerView";
import styles from "./Chat.module.css";
import { SourcePanel } from "./SourcePanel";

/** Диалог, которым поделился коллега (ТЗ §6): только чтение. */
export function SharedPage() {
  const { token = "" } = useParams();
  const shared = useQuery({
    queryKey: ["shared-conversation", token],
    queryFn: () =>
      unwrap(api.GET("/api/v1/conversations/shared/{token}", { params: { path: { token } } })),
  });
  const [opened, setOpened] = useState<OpenSource | null>(null);
  const trigger = useRef<HTMLElement | null>(null);
  useDocumentTitle(shared.data?.title ?? "Диалог коллеги");

  const close = useCallback(() => {
    setOpened(null);
    trigger.current?.focus();
  }, []);

  if (shared.isPending) return <PageSpinner />;
  if (shared.isError) {
    return (
      <EmptyState icon={<Link2Off size={32} aria-hidden />} title="Ссылка не открывается">
        <p>Автор закрыл доступ, или ссылка из другой компании.</p>
        <Link to="/" className={buttonClass("dark", "sm")}>
          Новый диалог
        </Link>
      </EmptyState>
    );
  }
  const data = shared.data;
  const sources = opened
    ? data.messages.find((m) => m.id === opened.messageId)?.sources
    : undefined;

  return (
    <section className={styles.page} aria-labelledby="chat-title">
      <header className={styles.chatHead}>
        <div className={styles.chatHeadText}>
          <h1 className={styles.chatTitle} id="chat-title">
            {data.title}
          </h1>
          <p className={`muted ${styles.chatMeta}`}>
            Автор: {data.owner_name} · {formatDateTime(data.shared_at)}
          </p>
        </div>
        <Link to="/" className={buttonClass("ghost", "sm")}>
          Задать свой вопрос
        </Link>
      </header>
      <div className={styles.log} role="log" aria-label="Переписка" tabIndex={0}>
        <div className={styles.thread}>
          {data.messages.map((message) =>
            message.role === "user" ? (
              <UserMessage key={message.id} message={message} busy />
            ) : (
              <AnswerBody
                key={message.id}
                message={message}
                openSource={opened}
                onOpenSource={(index) => {
                  trigger.current = document.activeElement as HTMLElement | null;
                  setOpened({ messageId: message.id, index });
                }}
              />
            ),
          )}
        </div>
      </div>
      {opened && sources?.[opened.index] ? (
        <SourcePanel sources={sources} index={opened.index} onClose={close} />
      ) : null}
    </section>
  );
}
