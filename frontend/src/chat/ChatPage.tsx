import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Share2 } from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";
import { useNavigate, useParams } from "react-router";

import { api, unwrap } from "../api/client";
import { ApiError, errorMessage } from "../api/errors";
import { isAdmin, useCompany, useMe } from "../auth/context";
import { useDocumentTitle } from "../lib/title";
import { Button } from "../ui/Button";
import { WindowMark } from "../ui/Logo";
import { Notice } from "../ui/Notice";
import { EmptyState } from "../ui/Page";
import { PageSpinner } from "../ui/Spinner";
import { useToast } from "../ui/useToast";
import { fetchConversation, pathUpTo, type Conversation, type Message } from "./api";
import { AnswerActions, AnswerBody, UserMessage, Versions, type OpenSource } from "./AnswerView";
import styles from "./Chat.module.css";
import { Composer, type Draft } from "./Composer";
import { conversationKey, SUGGESTIONS_KEY } from "./keys";
import { ShareDialog } from "./ShareDialog";
import { SourcePanel } from "./SourcePanel";
import { NEW_KEY, useChat, type LiveAnswer } from "./store";

/** Вопросы (ТЗ §6): новый диалог на «/», открытый — на «/c/:id». */
export function ChatPage() {
  const { conversationId } = useParams();
  return conversationId ? (
    <ConversationScreen key={conversationId} id={conversationId} />
  ) : (
    <NewConversation />
  );
}

/** Ошибка до начала ответа — над полем вопроса; текст остаётся в поле. */
function sendError(err: unknown): string {
  if (err instanceof ApiError && err.status === 402) {
    return "Лимит вопросов компании на этот месяц исчерпан. Новые вопросы станут доступны в следующем месяце.";
  }
  return errorMessage(err);
}

function isAbort(err: unknown): boolean {
  return err instanceof DOMException && err.name === "AbortError";
}

function NewConversation() {
  const me = useMe();
  const company = useCompany();
  const navigate = useNavigate();
  const { live, send } = useChat();
  const [error, setError] = useState<string | null>(null);
  useDocumentTitle("Вопросы");
  const pending = live[NEW_KEY];

  const ask = useCallback(
    async ({ question, attachments }: Draft): Promise<boolean> => {
      setError(null);
      try {
        await send(
          { kind: "new", question, attachmentIds: attachments.map((a) => a.id) },
          {
            attachments,
            onStarted: (id) => void navigate(`/c/${id}`, { replace: true }),
          },
        );
        return true;
      } catch (err) {
        if (!isAbort(err)) setError(sendError(err));
        return false;
      }
    },
    [navigate, send],
  );

  return (
    <section className={styles.page} aria-labelledby="chat-title">
      <h1 className="visually-hidden" id="chat-title">
        Новый диалог
      </h1>
      <div className={styles.log} role="log" aria-label="Переписка" tabIndex={0}>
        <div className={styles.thread}>
          {pending ? (
            <>
              <UserMessage
                message={{
                  id: "pending",
                  content: pending.question,
                  attachments: pending.attachments,
                }}
                busy
              />
              <PendingAnswer live={pending} />
            </>
          ) : (
            <div className={styles.welcome}>
              <span className={styles.welcomeMark}>
                <WindowMark size={40} />
              </span>
              <h2 className={styles.welcomeTitle}>
                {me.first_name ? `Здравствуйте, ${me.first_name}!` : "Здравствуйте!"}
              </h2>
              <p className={styles.welcomeText}>
                Я отвечаю по документам «{company.name}» и к каждому ответу прикладываю источник.
                Если в документах ответа нет — скажу об этом прямо.
              </p>
              <Suggestions onPick={(question) => void ask({ question, attachments: [] })} />
            </div>
          )}
        </div>
      </div>
      {error ? (
        <div className={styles.composerNotice}>
          <Notice kind="error">{error}</Notice>
        </div>
      ) : null}
      <Composer busy={Boolean(pending)} onSend={ask} />
    </section>
  );
}

function PendingAnswer({ live }: { live: LiveAnswer }) {
  return (
    <AnswerBody
      message={{
        id: "pending-answer",
        parent_id: null,
        role: "assistant",
        content: "",
        status: "generating",
        origin: null,
        sources: [],
        attachments: [],
        error_code: null,
        feedback: null,
        feedback_reason: null,
        feedback_comment: null,
        siblings: [],
        created_at: new Date().toISOString(),
      }}
      live={live}
      openSource={null}
      onOpenSource={() => undefined}
    />
  );
}

/** Подсказки на пустом экране: от администратора, затем частые вопросы. */
function Suggestions({ onPick }: { onPick: (question: string) => void }) {
  const suggestions = useQuery({
    queryKey: SUGGESTIONS_KEY,
    queryFn: () => unwrap(api.GET("/api/v1/suggestions")),
    staleTime: 5 * 60_000,
  });
  const seen = new Set<string>();
  const items = [
    ...(suggestions.data?.company.map((item) => item.text) ?? []),
    ...(suggestions.data?.frequent ?? []),
  ]
    .filter((text) => {
      const key = text.toLowerCase();
      if (seen.has(key)) return false;
      seen.add(key);
      return true;
    })
    .slice(0, 6);
  if (items.length === 0) return null;
  return (
    <ul className={styles.suggestions} aria-label="Подсказки">
      {items.map((text) => (
        <li key={text}>
          <button type="button" className={styles.suggestion} onClick={() => onPick(text)}>
            {text}
          </button>
        </li>
      ))}
    </ul>
  );
}

function ConversationScreen({ id }: { id: string }) {
  const me = useMe();
  const toast = useToast();
  const queryClient = useQueryClient();
  const { live: allLive, send, stop } = useChat();
  const live = allLive[id];
  const [error, setError] = useState<string | null>(null);
  const [opened, setOpened] = useState<OpenSource | null>(null);
  const [sharing, setSharing] = useState(false);
  const trigger = useRef<HTMLElement | null>(null);
  const bottom = useRef<HTMLDivElement>(null);

  const conversation = useQuery({
    queryKey: conversationKey(id),
    queryFn: () => fetchConversation(id),
    // Ответ пишется, а потока в этой вкладке нет (перезагрузка, обрыв) —
    // смотрим, не готов ли.
    refetchInterval: (query) =>
      !live && query.state.data?.messages.some((m) => m.status === "generating") ? 2000 : false,
  });
  const data = conversation.data;
  useDocumentTitle(data?.title ?? "Диалог");

  const messages = visibleMessages(data, live);
  const busy = Boolean(live) || messages.some((m) => m.status === "generating");
  const lastAnswer = [...messages].reverse().find((m) => m.role === "assistant");
  const count = messages.length + (live && !live.answerId ? 1 : 0);

  // Новый вопрос — прокручиваем вниз, к нему и ответу.
  useEffect(() => {
    bottom.current?.scrollIntoView({ block: "end", behavior: "smooth" });
  }, [count]);

  const run = useCallback(
    async (turn: Parameters<typeof send>[0], attachments: Draft["attachments"] = []) => {
      setError(null);
      try {
        await send(turn, { attachments });
        return true;
      } catch (err) {
        if (!isAbort(err)) setError(sendError(err));
        return false;
      }
    },
    [send],
  );

  async function select(messageId: string) {
    try {
      const next = await unwrap(
        api.PUT("/api/v1/conversations/{conversation_id}/current", {
          params: { path: { conversation_id: id } },
          body: { message_id: messageId },
        }),
      );
      queryClient.setQueryData<Conversation>(conversationKey(id), next);
    } catch (err) {
      toast.show(errorMessage(err), { tone: "error" });
    }
  }

  const openSource = useCallback((messageId: string, index: number) => {
    trigger.current = document.activeElement as HTMLElement | null;
    setOpened({ messageId, index });
  }, []);
  const closeSource = useCallback(() => {
    setOpened(null);
    trigger.current?.focus();
  }, []);
  const openedSource = opened
    ? messages.find((m) => m.id === opened.messageId)?.sources[opened.index]
    : undefined;

  if (conversation.isPending) return <PageSpinner />;
  if (conversation.isError || !data) {
    return (
      <EmptyState title="Диалог не найден">
        <p>Его удалили, или он принадлежит другому человеку.</p>
      </EmptyState>
    );
  }

  const announce = live
    ? live.stage === "writing"
      ? "Пишу ответ…"
      : "Ищу в документах…"
    : lastAnswer?.status === "failed"
      ? "Ошибка, ответа нет."
      : "";

  return (
    <section className={styles.page} aria-labelledby="chat-title">
      <header className={styles.chatHead}>
        <h1 className={styles.chatTitle} id="chat-title">
          {data.title}
        </h1>
        <Button variant="ghost" size="sm" onClick={() => setSharing(true)}>
          <Share2 size={16} aria-hidden /> <span className={styles.shareLabel}>Поделиться</span>
        </Button>
      </header>
      <div className={styles.log} role="log" aria-label="Переписка" tabIndex={0}>
        <div className={styles.thread}>
          {messages.map((message) =>
            message.role === "user" ? (
              <UserMessage
                key={message.id}
                message={message}
                busy={busy}
                onEdit={(question) =>
                  run(
                    {
                      kind: "ask",
                      conversationId: id,
                      parentId: message.parent_id,
                      question,
                      attachmentIds: message.attachments.map((a) => a.id),
                    },
                    message.attachments,
                  )
                }
                versions={
                  <Versions message={message} disabled={busy} onSelect={(m) => void select(m)} />
                }
              />
            ) : (
              <div key={message.id} className={styles.answerTurn}>
                <AnswerBody
                  message={message}
                  live={live?.answerId === message.id ? live : undefined}
                  openSource={opened}
                  onOpenSource={(index) => openSource(message.id, index)}
                  isAdmin={isAdmin(me)}
                />
                {message.status !== "generating" ? (
                  <AnswerActions
                    conversationId={id}
                    message={message}
                    busy={busy}
                    onRegenerate={() =>
                      void run({
                        kind: "regenerate",
                        conversationId: id,
                        questionId: message.parent_id ?? "",
                      })
                    }
                    versions={
                      <Versions
                        message={message}
                        disabled={busy}
                        onSelect={(m) => void select(m)}
                      />
                    }
                  />
                ) : null}
              </div>
            ),
          )}
          {live && !live.answerId ? (
            <>
              {live.regenerate ? null : (
                <UserMessage
                  message={{ id: "pending", content: live.question, attachments: live.attachments }}
                  busy
                />
              )}
              <PendingAnswer live={live} />
            </>
          ) : null}
          <div ref={bottom} />
        </div>
      </div>
      <p className="visually-hidden" aria-live="polite">
        {announce}
      </p>
      {error ? (
        <div className={styles.composerNotice}>
          <Notice kind="error">{error}</Notice>
        </div>
      ) : null}
      <Composer
        busy={busy}
        stopping={live?.stopping}
        onStop={live ? () => stop(id) : undefined}
        onSend={({ question, attachments }) =>
          run(
            {
              kind: "ask",
              conversationId: id,
              parentId: lastAnswer?.id ?? null,
              question,
              attachmentIds: attachments.map((a) => a.id),
            },
            attachments,
          )
        }
      />
      {opened && openedSource ? (
        <SourcePanel source={openedSource} number={opened.index + 1} onClose={closeSource} />
      ) : null}
      {sharing ? <ShareDialog conversation={data} onClose={() => setSharing(false)} /> : null}
    </section>
  );
}

/**
 * Что показать: ветку из кэша, а пока сервер не ответил start на новый
 * ход — ветку до точки хода (правка вопроса или повтор ответа обрезают её).
 */
function visibleMessages(data: Conversation | undefined, live: LiveAnswer | undefined): Message[] {
  const messages = data?.messages ?? [];
  if (!live || live.answerId) return messages;
  return pathUpTo(messages, live.parentId);
}
