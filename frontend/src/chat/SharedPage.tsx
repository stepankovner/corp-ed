import { useQuery } from "@tanstack/react-query";
import { Link2Off } from "lucide-react";
import { useCallback, useRef, useState } from "react";
import { Link, Navigate, useParams } from "react-router";

import { api, unwrap } from "../api/client";
import { useHashSecret } from "../auth/hashSecret";
import { formatDateTime } from "../lib/format";
import { useDocumentTitle } from "../lib/title";
import { buttonClass } from "../ui/buttonClass";
import { EmptyState } from "../ui/Page";
import { PageSpinner } from "../ui/Spinner";
import { AnswerBody, UserMessage, type OpenSource } from "./AnswerView";
import styles from "./Chat.module.css";
import { pendingShare } from "./pendingShare";
import { SourcePanel } from "./SourcePanel";

/**
 * Диалог, которым поделился коллега (ТЗ §6): только чтение.
 *
 * Ссылка — /shared#<токен>: фрагмент не уходит на сервер и в журналы,
 * страница забирает токен и сразу убирает его из адресной строки (как
 * приглашение). Токен уходит телом запроса, не в адресе. Гостя вход
 * возвращает сюда без фрагмента — токен ждёт в хранилище вкладки
 * (RequireAuth), там же он и для перезагрузки страницы.
 */
export function SharedPage() {
  const fromHash = useHashSecret(null);
  const [token] = useState(() => fromHash ?? pendingShare());
  const shared = useQuery({
    queryKey: ["shared-conversation", token],
    queryFn: () =>
      unwrap(api.POST("/api/v1/conversations/shared/open", { body: { token: token ?? "" } })),
    enabled: token !== null,
  });
  const [opened, setOpened] = useState<OpenSource | null>(null);
  const trigger = useRef<HTMLElement | null>(null);
  useDocumentTitle(shared.data?.title ?? "Диалог коллеги");

  const close = useCallback(() => {
    setOpened(null);
    trigger.current?.focus();
  }, []);

  if (token !== null && shared.isPending) return <PageSpinner />;
  if (token === null || shared.isError || !shared.data) {
    return (
      <EmptyState icon={<Link2Off size={32} aria-hidden />} title="Ссылка не открывается">
        <p>Автор закрыл доступ, срок ссылки истёк, или она из другой компании.</p>
        <Link to="/" className={buttonClass("dark", "sm")}>
          Новый диалог
        </Link>
      </EmptyState>
    );
  }
  const data = shared.data;
  const openedMessage = opened ? data.messages.find((m) => m.id === opened.messageId) : undefined;
  const sources = openedMessage?.sources;

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
        <SourcePanel
          sources={sources}
          content={openedMessage?.content ?? ""}
          index={opened.index}
          onClose={close}
        />
      ) : null}
    </section>
  );
}

/** Ссылка старого вида /shared/<токен>: токен — во фрагмент, как у новых. */
export function LegacySharedRedirect() {
  const { token = "" } = useParams();
  return <Navigate to={{ pathname: "/shared", hash: token }} replace />;
}
