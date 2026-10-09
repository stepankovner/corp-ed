import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Plug, Share2, X } from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";
import { Link, useNavigate, useParams } from "react-router";

import { api, unwrap } from "../api/client";
import { ApiError, errorMessage } from "../api/errors";
import { isAdmin, useCompany, useMe } from "../auth/context";
import { useDocumentTitle } from "../lib/title";
import { Button } from "../ui/Button";
import { IconButton } from "../ui/IconButton";
import { WindowMark } from "../ui/Logo";
import { Notice } from "../ui/Notice";
import { EmptyState } from "../ui/Page";
import { PageSpinner } from "../ui/Spinner";
import { FirstSteps } from "../onboarding/FirstSteps";
import { useToast } from "../ui/useToast";
import { fetchConversation, pathUpTo, type Conversation, type Message } from "./api";
import { AnswerActions, AnswerBody, UserMessage, Versions, type OpenSource } from "./AnswerView";
import styles from "./Chat.module.css";
import { Composer, type Draft } from "./Composer";
import { CreditsStopped } from "./CreditsStopped";
import { conversationKey, SUGGESTIONS_KEY } from "./keys";
import { ShareDialog } from "./ShareDialog";
import { SourcePanel } from "./SourcePanel";
import { HIDDEN_CONNECT_KEY, NEW_KEY, useChat, type LiveAnswer } from "./store";

/** Вопросы (ТЗ §6): новый диалог на «/», открытый — на «/c/:id». */
export function ChatPage() {
  const { conversationId } = useParams();
  return conversationId ? (
    <ConversationScreen key={conversationId} id={conversationId} />
  ) : (
    <NewConversation />
  );
}

/**
 * Ошибка до начала ответа — над полем вопроса; текст остаётся в поле.
 * Кончились кредиты компании (402) — не текст, а блок с действием
 * (CreditsStopped); дневной лимит (429) — текст сервера.
 */
type SendProblem = { credits: true } | { credits: false; text: string };

function sendError(err: unknown): SendProblem {
  if (err instanceof ApiError && err.status === 402) return { credits: true };
  return { credits: false, text: errorMessage(err) };
}

function ProblemNotice({ problem, admin }: { problem: SendProblem; admin: boolean }) {
  return (
    <div className={styles.composerNotice}>
      {problem.credits ? (
        <CreditsStopped admin={admin} />
      ) : (
        <Notice kind="error">{problem.text}</Notice>
      )}
    </div>
  );
}

function isAbort(err: unknown): boolean {
  return err instanceof DOMException && err.name === "AbortError";
}

function NewConversation() {
  const me = useMe();
  const company = useCompany();
  const navigate = useNavigate();
  const { live, send } = useChat();
  const [error, setError] = useState<SendProblem | null>(null);
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
              <ConnectBanner />
              <FirstSteps />
              <Suggestions onPick={(question) => void ask({ question, attachments: [] })} />
            </div>
          )}
        </div>
      </div>
      {error ? <ProblemNotice problem={error} admin={isAdmin(me)} /> : null}
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

function readHidden(): string[] {
  try {
    const value: unknown = JSON.parse(localStorage.getItem(HIDDEN_CONNECT_KEY) ?? "[]");
    return Array.isArray(value) ? value.filter((item) => typeof item === "string") : [];
  } catch {
    return [];
  }
}

/**
 * Источник, который сотрудник подключает своим аккаунтом (ТЗ §5), ещё не
 * подключён — предложить на пустом экране. «Скрыть» запоминается в
 * браузере для этого источника.
 */
function ConnectBanner() {
  const [hidden, setHidden] = useState(readHidden);
  const mine = useQuery({
    queryKey: ["connectors", "mine"],
    queryFn: () => unwrap(api.GET("/api/v1/connectors/mine")),
    staleTime: 5 * 60_000,
  });
  const item = mine.data?.find(
    (connector) =>
      (connector.oauth || connector.credential_fields.length > 0) &&
      connector.grant_status !== "active" &&
      !hidden.includes(connector.id),
  );
  if (!item) return null;

  function hide(id: string) {
    const next = [...hidden, id];
    setHidden(next);
    try {
      localStorage.setItem(HIDDEN_CONNECT_KEY, JSON.stringify(next));
    } catch {
      // Хранилище недоступно (приватный режим) — скрываем до перезагрузки.
    }
  }

  return (
    <div className={styles.connect} role="note">
      <Plug size={18} aria-hidden className={styles.connectIcon} />
      <p className={styles.connectText}>
        {item.grant_status
          ? `Доступ к вашему аккаунту ${item.name} больше не действует — подключите его заново, чтобы ассистент искал и по вашим файлам.`
          : `Подключите свой ${item.name}, чтобы ассистент искал и по вашим файлам.`}{" "}
        <Link to="/settings/connections">Подключить</Link>
      </p>
      <IconButton size="sm" label="Скрыть" onClick={() => hide(item.id)}>
        <X size={16} aria-hidden />
      </IconButton>
    </div>
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
  const [error, setError] = useState<SendProblem | null>(null);
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
  const openedMessage = opened ? messages.find((m) => m.id === opened.messageId) : undefined;
  const openedSources = openedMessage?.sources;

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
      {error ? <ProblemNotice problem={error} admin={isAdmin(me)} /> : null}
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
      {opened && openedSources?.[opened.index] ? (
        <SourcePanel
          sources={openedSources}
          content={openedMessage?.content ?? ""}
          index={opened.index}
          onClose={closeSource}
        />
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
