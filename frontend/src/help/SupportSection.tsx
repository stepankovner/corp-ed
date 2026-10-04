import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useId, useState, type SubmitEvent } from "react";

import { api, unwrap, type Schemas } from "../api/client";
import { ApiError, errorMessage } from "../api/errors";
import { useMe } from "../auth/context";
import { formatDateTime, formatNumber } from "../lib/format";
import { Badge, type Tone } from "../ui/Badge";
import { Button } from "../ui/Button";
import { SelectField, TextAreaField } from "../ui/Field";
import { Notice } from "../ui/Notice";
import styles from "./Help.module.css";

type Topic = Schemas["SupportCreateRequest"]["topic"];
type SupportRequest = Schemas["SupportResponse"];

const TOPICS: { value: Topic; label: string }[] = [
  { value: "login", label: "Вход и учётная запись" },
  { value: "documents", label: "Документы и подключения" },
  { value: "answers", label: "Ответы ассистента" },
  { value: "billing", label: "Тариф и оплата" },
  { value: "other", label: "Другое" },
];

const STATUS: Record<SupportRequest["status"], { label: string; tone: Tone }> = {
  new: { label: "новое", tone: "accent" },
  answered: { label: "отвечено", tone: "ok" },
  closed: { label: "закрыто", tone: "muted" },
};

/** Как на сервере (SupportCreateRequest): 10–4000 символов. */
const MIN_MESSAGE = 10;
const MAX_MESSAGE = 4000;

const MINE_KEY = ["support", "mine"] as const;

/** Номер обращения для человека — первые 8 знаков id, как в письме и у команды. */
function number(id: string): string {
  return id.slice(0, 8);
}

function topicLabel(topic: Topic): string {
  return TOPICS.find((item) => item.value === topic)?.label ?? topic;
}

function sendError(error: unknown): string {
  if (error instanceof ApiError && error.status === 429) {
    return "Вы уже отправили несколько обращений за час. Попробуйте позже.";
  }
  return errorMessage(error);
}

/**
 * «Написать в поддержку» (ТЗ §8): обращение уходит команде kronto, ответ
 * приходит на почту учётки. Работает и без компании. Ниже — свои
 * обращения и их состояние.
 */
export function SupportSection({ id }: { id: string }) {
  const me = useMe();
  const queryClient = useQueryClient();
  const [topic, setTopic] = useState<Topic | "">("");
  const [message, setMessage] = useState("");
  // Ошибки полей — после первой попытки отправить, дальше — по мере ввода.
  const [tried, setTried] = useState(false);
  const [sent, setSent] = useState<SupportRequest | null>(null);
  const text = message.trim();
  const over = message.length > MAX_MESSAGE;

  const topicError = tried && !topic ? "Выберите тему." : null;
  const messageError = !tried
    ? null
    : !text
      ? "Опишите, что случилось."
      : text.length < MIN_MESSAGE
        ? `Напишите чуть подробнее — хотя бы ${MIN_MESSAGE} символов.`
        : over
          ? `Сократите до ${formatNumber(MAX_MESSAGE)} символов.`
          : null;

  const send = useMutation({
    mutationFn: (body: Schemas["SupportCreateRequest"]) =>
      unwrap(api.POST("/api/v1/support", { body })),
    onSuccess: (created) => {
      setSent(created);
      setTopic("");
      setMessage("");
      setTried(false);
      queryClient.setQueryData<SupportRequest[]>(MINE_KEY, (old) => [
        created,
        ...(old ?? []).filter((item) => item.id !== created.id),
      ]);
    },
  });

  function submit(event: SubmitEvent) {
    event.preventDefault();
    setTried(true);
    if (!topic || text.length < MIN_MESSAGE || over) return;
    setSent(null);
    send.mutate({ topic, message: text });
  }

  return (
    <section id={id} className={styles.support} aria-labelledby={`${id}-title`}>
      <div>
        <h2 id={`${id}-title`} className={styles.supportTitle} tabIndex={-1}>
          Написать в поддержку
        </h2>
        <p className={styles.supportText}>
          Не нашли ответа или что-то не работает — напишите команде kronto.
        </p>
      </div>
      {sent ? (
        <Notice kind="ok" title={`Обращение №${number(sent.id)} отправлено`}>
          Ответим на почту {me.email}.
        </Notice>
      ) : null}
      {send.isError ? <Notice kind="error">{sendError(send.error)}</Notice> : null}
      <form className={styles.form} onSubmit={submit} noValidate>
        <SelectField
          label="Тема"
          required
          value={topic}
          onChange={(e) => setTopic(TOPICS.find((t) => t.value === e.target.value)?.value ?? "")}
          error={topicError}
        >
          <option value="" disabled>
            Выберите тему
          </option>
          {TOPICS.map((item) => (
            <option key={item.value} value={item.value}>
              {item.label}
            </option>
          ))}
        </SelectField>
        <TextAreaField
          label="Сообщение"
          required
          rows={5}
          placeholder="Что случилось? Что вы делали и что ожидали увидеть?"
          value={message}
          onChange={(e) => setMessage(e.target.value)}
          error={messageError}
          hint={
            <span className={styles.hintRow}>
              <span>Ответим на почту {me.email}. Пароли и коды из писем не присылайте.</span>
              <span className={`num ${styles.counter} ${over ? styles.over : ""}`} aria-hidden>
                {formatNumber(message.length)} / {formatNumber(MAX_MESSAGE)}
              </span>
            </span>
          }
        />
        <div>
          <Button type="submit" size="sm" busy={send.isPending}>
            Отправить
          </Button>
        </div>
      </form>
      <MyRequests />
    </section>
  );
}

function MyRequests() {
  const titleId = useId();
  const mine = useQuery({
    queryKey: MINE_KEY,
    queryFn: () => unwrap(api.GET("/api/v1/support/mine")),
  });
  if (!mine.data?.length) return null;
  return (
    <section className={styles.mine} aria-labelledby={titleId}>
      <h3 id={titleId} className={styles.mineTitle}>
        Ваши обращения
      </h3>
      <ul className={styles.requests}>
        {mine.data.map((item) => {
          const status = STATUS[item.status];
          return (
            <li key={item.id} className={styles.request}>
              <div className={styles.requestHead}>
                <span className={styles.requestTopic}>{topicLabel(item.topic)}</span>
                <Badge tone={status.tone}>{status.label}</Badge>
              </div>
              <p className={styles.requestText}>{item.message}</p>
              <p className={`mono muted ${styles.requestMeta}`}>
                №{number(item.id)} · {formatDateTime(item.created_at)}
              </p>
            </li>
          );
        })}
      </ul>
      <p className={`muted ${styles.small}`}>Ответы приходят на почту.</p>
    </section>
  );
}
