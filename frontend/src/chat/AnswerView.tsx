import { useMutation, useQueryClient } from "@tanstack/react-query";
import {
  Check,
  ChevronLeft,
  ChevronRight,
  Copy,
  FileText,
  Pencil,
  RotateCcw,
  ThumbsDown,
  ThumbsUp,
  X,
} from "lucide-react";
import { useState, type ReactNode, type SubmitEvent } from "react";
import { Link } from "react-router";

import { api, unwrap } from "../api/client";
import { errorMessage } from "../api/errors";
import { Badge } from "../ui/Badge";
import { Button } from "../ui/Button";
import { IconButton } from "../ui/IconButton";
import { Spinner } from "../ui/Spinner";
import { useToast } from "../ui/useToast";
import type { Conversation, FeedbackReason, Message, Source } from "./api";
import { FEEDBACK_REASONS } from "./feedbackReasons";
import styles from "./Chat.module.css";
import { stripGeneralPrefix } from "./citations";
import { conversationKey } from "./keys";
import { Markdown } from "./Markdown";
import { sourceSection } from "./sources";
import type { LiveAnswer } from "./store";

export interface OpenSource {
  messageId: string;
  index: number;
}

function CitationButton({
  n,
  sources,
  onOpen,
}: {
  n: number;
  sources: Source[];
  onOpen: (index: number) => void;
}) {
  const source = sources[n - 1];
  if (!source) return <>[{n}]</>;
  return (
    <button
      type="button"
      className={styles.src}
      onClick={() => onOpen(n - 1)}
      aria-label={`Источник ${n}: ${source.title}`}
      aria-controls="source-panel"
    >
      {n}
    </button>
  );
}

const ERRORS: Record<string, string> = {
  llm_unavailable: "Сервис ответов временно недоступен.",
  timeout: "Ответ занял слишком много времени.",
  interrupted: "Ответ прервался: сервис перезапускался.",
  internal: "Не удалось получить ответ.",
};

/**
 * Текст ответа: по документам — со ссылками на источники, общий — с
 * плашкой «не из документов», отказ — отдельным блоком. Для ответа,
 * который печатается, — текст из потока.
 */
export function AnswerBody({
  message,
  live,
  openSource,
  onOpenSource,
  isAdmin = false,
}: {
  message: Message;
  live?: LiveAnswer;
  openSource: OpenSource | null;
  onOpenSource: (index: number) => void;
  isAdmin?: boolean;
}) {
  const streaming = message.status === "generating";
  const origin = live?.origin ?? message.origin;
  const raw = live ? live.text : message.content;

  if (streaming && !raw) {
    return (
      <div className={`${styles.msg} ${styles.bot} ${styles.searching}`}>
        <Spinner size={14} />
        <span className={styles.searchingText}>
          {!live
            ? "Ответ ещё пишется"
            : live.stage === "writing" && origin === "general_knowledge"
              ? "В документах ответа нет, пишу общий ответ"
              : live.stage === "writing"
                ? "Пишу ответ"
                : "Ищу в документах"}
        </span>
      </div>
    );
  }

  if (message.status === "failed" && !raw) {
    return <FailedBody message={message} isAdmin={isAdmin} />;
  }

  if (origin === "none" && !streaming) {
    return (
      <div className={`${styles.msg} ${styles.bot} ${styles.refusal}`}>
        <p className={styles.refusalTitle}>В документах компании нет ответа на этот вопрос.</p>
        <p className="muted">Уточните у руководителя или в профильном отделе.</p>
        <Badge tone="warn" wrap>
          Вопрос попадёт в отчёт о пробелах в документах
        </Badge>
      </div>
    );
  }

  const general = origin === "general_knowledge";
  const content = general ? stripGeneralPrefix(raw) : raw;
  const sources = live ? [] : message.sources;
  return (
    <div className={`${styles.msg} ${styles.bot}`}>
      {general ? (
        <div className={styles.general} style={{ marginBottom: 12 }}>
          <p className={styles.refusalTitle}>В документах компании ответа нет.</p>
          <Badge tone="warn" wrap>
            Ниже — общая информация, не из документов компании
          </Badge>
        </div>
      ) : null}
      <div className={streaming ? styles.streaming : undefined}>
        <Markdown
          renderCitation={
            general || streaming
              ? undefined
              : (n) => <CitationButton n={n} sources={sources} onOpen={onOpenSource} />
          }
        >
          {content}
        </Markdown>
      </div>
      {message.status === "stopped" ? (
        <p className={`muted ${styles.note}`}>Ответ остановлен.</p>
      ) : message.status === "failed" ? (
        <p className={`${styles.note} ${styles.noteError}`}>
          {ERRORS[message.error_code ?? ""] ?? ERRORS.internal} Ответ не дописан.
        </p>
      ) : null}
      {sources.length > 0 && !general ? (
        <div className={styles.sources}>
          {sources.map((source, index) => {
            const expanded = openSource?.messageId === message.id && openSource.index === index;
            return (
              <button
                key={`${source.material_id ?? source.attachment_id}-${source.position}-${index}`}
                type="button"
                className={styles.sourceBtn}
                aria-expanded={expanded}
                aria-controls="source-panel"
                onClick={() => onOpenSource(index)}
              >
                <span className={`${styles.src} ${styles.srcStatic}`} aria-hidden>
                  {index + 1}
                </span>
                <span className={styles.sourceText}>
                  <span className="visually-hidden">Источник {index + 1}: </span>
                  <span className={styles.sourceTitle}>{source.title}</span>
                  <span className={`mono ${styles.sourceMeta}`}>
                    {source.kind === "attachment"
                      ? "ваш файл"
                      : source.content === null
                        ? "недоступен"
                        : sourceSection(source) || `фрагмент ${source.position + 1}`}
                  </span>
                </span>
              </button>
            );
          })}
        </div>
      ) : null}
    </div>
  );
}

function FailedBody({ message, isAdmin }: { message: Message; isAdmin: boolean }) {
  if (message.error_code === "credits_exhausted") {
    return (
      <div className={`${styles.msg} ${styles.limit}`} role="alert">
        <p className={styles.refusalTitle}>Лимит вопросов компании на этот месяц исчерпан.</p>
        <p>
          {isAdmin ? (
            <>
              Расход и дату обновления лимита видно в разделе{" "}
              <Link to="/admin/tariff">«Тариф»</Link>.
            </>
          ) : (
            "Новые вопросы станут доступны в следующем месяце. Если ответ нужен срочно — напишите администратору."
          )}
        </p>
      </div>
    );
  }
  return (
    <div className={`${styles.msg} ${styles.error}`} role="alert">
      <p>{ERRORS[message.error_code ?? ""] ?? ERRORS.internal} Попробуйте ответить заново.</p>
    </div>
  );
}

/** Версии сообщения после правок и повторов: ‹ 2/3 ›. */
export function Versions({
  message,
  disabled,
  onSelect,
}: {
  message: Message;
  disabled: boolean;
  onSelect: (id: string) => void;
}) {
  const index = message.siblings.indexOf(message.id);
  const total = message.siblings.length;
  const previous = message.siblings[index - 1];
  const next = message.siblings[index + 1];
  if (total < 2 || index < 0) return null;
  const what = message.role === "user" ? "вопроса" : "ответа";
  return (
    <span className={styles.versions}>
      <IconButton
        size="sm"
        label={`Предыдущая версия ${what}`}
        disabled={disabled || !previous}
        onClick={() => previous && onSelect(previous)}
      >
        <ChevronLeft size={16} aria-hidden />
      </IconButton>
      <span className="mono" aria-label={`Версия ${index + 1} из ${total}`}>
        {index + 1}/{total}
      </span>
      <IconButton
        size="sm"
        label={`Следующая версия ${what}`}
        disabled={disabled || !next}
        onClick={() => next && onSelect(next)}
      >
        <ChevronRight size={16} aria-hidden />
      </IconButton>
    </span>
  );
}

function CopyButton({ text, label }: { text: string; label: string }) {
  const [copied, setCopied] = useState(false);
  const toast = useToast();
  async function copy() {
    try {
      await navigator.clipboard.writeText(text);
      setCopied(true);
      toast.show("Скопировано");
      window.setTimeout(() => setCopied(false), 1600);
    } catch {
      toast.show("Не удалось скопировать: браузер не дал доступа к буферу обмена", {
        tone: "error",
      });
    }
  }
  return (
    <IconButton size="sm" label={label} onClick={() => void copy()}>
      {copied ? <Check size={16} aria-hidden /> : <Copy size={16} aria-hidden />}
    </IconButton>
  );
}

interface FeedbackValue {
  value: 1 | -1 | null;
  reason?: FeedbackReason | null;
  comment?: string | null;
}

/** Действия под ответом: копировать, оценка с «что не так», ответить заново. */
export function AnswerActions({
  conversationId,
  message,
  busy,
  onRegenerate,
  versions,
}: {
  conversationId: string;
  message: Message;
  busy: boolean;
  onRegenerate: () => void;
  versions: ReactNode;
}) {
  const queryClient = useQueryClient();
  const toast = useToast();
  const [asking, setAsking] = useState(false);
  const canRate = message.status === "complete" || message.status === "stopped";
  const content =
    message.origin === "general_knowledge" ? stripGeneralPrefix(message.content) : message.content;

  const rate = useMutation({
    mutationFn: (body: FeedbackValue) =>
      unwrap(
        api.PUT("/api/v1/conversations/{conversation_id}/messages/{message_id}/feedback", {
          params: { path: { conversation_id: conversationId, message_id: message.id } },
          body: { value: body.value, reason: body.reason ?? null, comment: body.comment ?? null },
        }),
      ),
    onMutate: (body) => {
      const key = conversationKey(conversationId);
      const previous = queryClient.getQueryData<Conversation>(key);
      queryClient.setQueryData<Conversation>(key, (old) =>
        old
          ? {
              ...old,
              messages: old.messages.map((item) =>
                item.id === message.id
                  ? {
                      ...item,
                      feedback: body.value,
                      feedback_reason: body.reason ?? null,
                      feedback_comment: body.comment ?? null,
                    }
                  : item,
              ),
            }
          : old,
      );
      return { previous };
    },
    onError: (err, _body, context) => {
      if (context?.previous) {
        queryClient.setQueryData(conversationKey(conversationId), context.previous);
      }
      toast.show(errorMessage(err), { tone: "error" });
    },
  });

  function like() {
    setAsking(false);
    rate.mutate({ value: message.feedback === 1 ? null : 1 });
  }

  function dislike() {
    if (message.feedback === -1) {
      setAsking(false);
      rate.mutate({ value: null });
      return;
    }
    rate.mutate({ value: -1 });
    setAsking(true);
  }

  return (
    <>
      <div className={styles.actions}>
        {content ? <CopyButton text={content} label="Скопировать ответ" /> : null}
        {canRate ? (
          <>
            <IconButton
              size="sm"
              label="Ответ помог"
              aria-pressed={message.feedback === 1}
              active={message.feedback === 1}
              onClick={like}
            >
              <ThumbsUp size={16} aria-hidden />
            </IconButton>
            <IconButton
              size="sm"
              label="Ответ не помог"
              aria-pressed={message.feedback === -1}
              active={message.feedback === -1}
              onClick={dislike}
            >
              <ThumbsDown size={16} aria-hidden />
            </IconButton>
          </>
        ) : null}
        <IconButton size="sm" label="Ответить заново" disabled={busy} onClick={onRegenerate}>
          <RotateCcw size={16} aria-hidden />
        </IconButton>
        {versions}
        {message.feedback && !asking ? (
          <span className={styles.thanks}>Спасибо за оценку</span>
        ) : null}
      </div>
      {asking ? (
        <FeedbackForm
          initial={message}
          busy={rate.isPending}
          onClose={() => setAsking(false)}
          onSubmit={(reason, comment) =>
            rate.mutate(
              { value: -1, reason, comment },
              {
                onSuccess: () => {
                  setAsking(false);
                  toast.show("Спасибо, учтём");
                },
              },
            )
          }
        />
      ) : null}
    </>
  );
}

/** «Что не так?» — причина и комментарий к 👎 (ТЗ §6). */
function FeedbackForm({
  initial,
  busy,
  onClose,
  onSubmit,
}: {
  initial: Message;
  busy: boolean;
  onClose: () => void;
  onSubmit: (reason: FeedbackReason | null, comment: string | null) => void;
}) {
  const [reason, setReason] = useState<FeedbackReason | null>(initial.feedback_reason);
  const [comment, setComment] = useState(initial.feedback_comment ?? "");

  function submit(event: SubmitEvent) {
    event.preventDefault();
    onSubmit(reason, comment.trim() || null);
  }

  return (
    <form className={styles.feedback} onSubmit={submit} aria-label="Что не так с ответом">
      <div className={styles.feedbackHead}>
        <p className={styles.feedbackTitle}>Что не так с ответом?</p>
        <IconButton size="sm" label="Закрыть" tooltip={false} onClick={onClose}>
          <X size={16} aria-hidden />
        </IconButton>
      </div>
      <div className={styles.reasons} role="group" aria-label="Причина">
        {FEEDBACK_REASONS.map((item) => (
          <button
            key={item.value}
            type="button"
            className={styles.reason}
            aria-pressed={reason === item.value}
            onClick={() => setReason(reason === item.value ? null : item.value)}
          >
            {item.label}
          </button>
        ))}
      </div>
      <label className="visually-hidden" htmlFor={`feedback-${initial.id}`}>
        Комментарий
      </label>
      <textarea
        id={`feedback-${initial.id}`}
        className={styles.feedbackText}
        rows={2}
        maxLength={1000}
        placeholder="Что было бы правильно? Администратор увидит это без вашего имени."
        value={comment}
        onChange={(e) => setComment(e.target.value)}
      />
      <div className={styles.feedbackFoot}>
        <Button variant="ghost" size="xs" onClick={onClose}>
          Пропустить
        </Button>
        <Button type="submit" size="xs" busy={busy} disabled={!reason && !comment.trim()}>
          Отправить
        </Button>
      </div>
    </form>
  );
}

/** Вопрос сотрудника: файлы, правка, версии. */
export function UserMessage({
  message,
  busy,
  onEdit,
  versions,
}: {
  message: Pick<Message, "id" | "content" | "attachments">;
  busy: boolean;
  onEdit?: (question: string) => Promise<boolean>;
  versions?: ReactNode;
}) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(message.content);
  const [error, setError] = useState<string | null>(null);
  const [sending, setSending] = useState(false);
  const question = draft.trim();

  async function save(event: SubmitEvent) {
    event.preventDefault();
    if (!onEdit || !question) return;
    setSending(true);
    setError(null);
    try {
      if (await onEdit(question)) setEditing(false);
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setSending(false);
    }
  }

  return (
    <div className={styles.userTurn}>
      {message.attachments.length > 0 ? (
        <ul className={styles.attachedList} aria-label="Файлы к вопросу">
          {message.attachments.map((file) => (
            <li key={file.id} className={styles.attached}>
              <FileText size={16} aria-hidden />
              <span className={styles.attachedName}>{file.filename}</span>
            </li>
          ))}
        </ul>
      ) : null}
      {editing ? (
        <form className={styles.editForm} onSubmit={(e) => void save(e)}>
          <label className="visually-hidden" htmlFor={`edit-${message.id}`}>
            Изменить вопрос
          </label>
          <textarea
            id={`edit-${message.id}`}
            className={styles.editInput}
            value={draft}
            maxLength={4000}
            rows={3}
            onChange={(e) => setDraft(e.target.value)}
            autoFocus
          />
          {error ? <p className={styles.noteError}>{error}</p> : null}
          <div className={styles.feedbackFoot}>
            <Button variant="ghost" size="xs" onClick={() => setEditing(false)}>
              Отмена
            </Button>
            <Button type="submit" size="xs" busy={sending} disabled={!question || busy}>
              Отправить
            </Button>
          </div>
        </form>
      ) : (
        <div className={`${styles.msg} ${styles.user}`}>{message.content}</div>
      )}
      {!editing ? (
        <div className={`${styles.actions} ${styles.userActions}`}>
          <CopyButton text={message.content} label="Скопировать вопрос" />
          {onEdit ? (
            <IconButton
              size="sm"
              label="Изменить вопрос"
              disabled={busy}
              onClick={() => {
                setDraft(message.content);
                setEditing(true);
              }}
            >
              <Pencil size={16} aria-hidden />
            </IconButton>
          ) : null}
          {versions}
        </div>
      ) : null}
    </div>
  );
}
