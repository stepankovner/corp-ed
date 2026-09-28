import { Check, Copy, RotateCcw, ThumbsDown, ThumbsUp } from "lucide-react";
import { useState } from "react";
import { Link } from "react-router";

import { Badge } from "../ui/Badge";
import { Button } from "../ui/Button";
import { IconButton } from "../ui/IconButton";
import styles from "./Chat.module.css";
import { stripGeneralPrefix } from "./citations";
import { Markdown } from "./Markdown";
import { sourceSection, type Source } from "./sources";
import type { Turn, Vote } from "./store";

type Done = Extract<Turn, { state: "done" }>;
type Failed = Extract<Turn, { state: "error" }>;

interface AnswerProps {
  turn: Done;
  openSource: { turnId: string; index: number } | null;
  onOpenSource: (index: number) => void;
  onVote: (value: Vote) => void;
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

export function AnswerView({ turn, openSource, onOpenSource, onVote }: AnswerProps) {
  const { answer } = turn;
  const sources = answer.sources;

  if (answer.origin === "none") {
    return (
      <div className={`${styles.msg} ${styles.bot} ${styles.refusal}`}>
        <p className={styles.refusalTitle}>В документах компании нет ответа на этот вопрос.</p>
        <p className="muted">Уточните у руководителя или в отделе, который отвечает за эту тему.</p>
        <Badge tone="warn" wrap>
          Вопрос попадёт в отчёт о пробелах в документах
        </Badge>
      </div>
    );
  }

  const general = answer.origin === "general_knowledge";
  const content = general ? stripGeneralPrefix(answer.content) : answer.content;

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
      <Markdown
        renderCitation={
          general
            ? undefined
            : (n) => <CitationButton n={n} sources={sources} onOpen={onOpenSource} />
        }
      >
        {content}
      </Markdown>
      {sources.length > 0 && !general ? (
        <div className={styles.sources}>
          {sources.map((source, index) => {
            const expanded = openSource?.turnId === turn.id && openSource.index === index;
            return (
              <button
                key={`${source.material_id}-${source.position}`}
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
                    {sourceSection(source) || `фрагмент ${source.position + 1}`}
                  </span>
                </span>
              </button>
            );
          })}
        </div>
      ) : null}
      <AnswerActions turn={turn} content={content} onVote={onVote} />
    </div>
  );
}

function AnswerActions({
  turn,
  content,
  onVote,
}: {
  turn: Done;
  content: string;
  onVote: (value: Vote) => void;
}) {
  const [copied, setCopied] = useState(false);
  const canVote = Boolean(turn.answer.answer_id);

  async function copy() {
    try {
      await navigator.clipboard.writeText(content);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1600);
    } catch {
      // Буфер обмена недоступен (нет разрешения) — молча.
    }
  }

  return (
    <div className={styles.actions}>
      <IconButton
        size="sm"
        label={copied ? "Скопировано" : "Скопировать ответ"}
        onClick={() => void copy()}
      >
        {copied ? <Check size={16} aria-hidden /> : <Copy size={16} aria-hidden />}
      </IconButton>
      {canVote ? (
        <>
          <IconButton
            size="sm"
            label="Ответ помог"
            aria-pressed={turn.vote === 1}
            active={turn.vote === 1}
            onClick={() => onVote(1)}
          >
            <ThumbsUp size={16} aria-hidden />
          </IconButton>
          <IconButton
            size="sm"
            label="Ответ не помог"
            aria-pressed={turn.vote === -1}
            active={turn.vote === -1}
            onClick={() => onVote(-1)}
          >
            <ThumbsDown size={16} aria-hidden />
          </IconButton>
          {turn.vote ? <span className={styles.thanks}>Спасибо за оценку</span> : null}
        </>
      ) : null}
    </div>
  );
}

export function FailedView({
  turn,
  isAdmin,
  onRetry,
}: {
  turn: Failed;
  isAdmin: boolean;
  onRetry: () => void;
}) {
  if (turn.status === 402) {
    return (
      <div className={`${styles.msg} ${styles.limit}`} role="alert">
        <p className={styles.refusalTitle}>Лимит вопросов компании на этот месяц исчерпан.</p>
        <p>
          {isAdmin ? (
            <>
              Расход и дату обновления лимита видно в разделе{" "}
              <Link to="/admin/usage">«Лимит вопросов»</Link>.
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
      <p>{turn.message}</p>
      {turn.status !== 422 ? (
        <Button variant="ghost" size="xs" onClick={onRetry}>
          <RotateCcw size={14} aria-hidden /> Повторить
        </Button>
      ) : null}
    </div>
  );
}
