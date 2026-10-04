import { useQuery } from "@tanstack/react-query";
import { ArrowUp, FileText } from "lucide-react";
import { useEffect, useId, useRef, useState, type SubmitEvent } from "react";
import { Link } from "react-router";

import { api, unwrap, type Schemas } from "../api/client";
import { ApiError, errorMessage } from "../api/errors";
import { Markdown } from "../chat/Markdown";
import { readEvents } from "../chat/sse";
import { buttonClass } from "../ui/buttonClass";
import { Notice } from "../ui/Notice";
import { Skeleton } from "../ui/Skeleton";
import { Spinner } from "../ui/Spinner";
import { DEMO_COMPANY, SCENARIOS } from "./demoScenarios";
import { SITE, useSiteTitle } from "./meta";
import styles from "./Sandbox.module.css";
import { SectionHead, SiteLayout } from "./SiteLayout";
import site from "./Site.module.css";

type Answer = Schemas["DemoAnswerResponse"];
type DemoEvent = Schemas["DemoStreamEvent"];

/** Как на сервере (DemoQuestionRequest). */
const MAX_QUESTION = 300;
const MIN_QUESTION = 3;
/** Пока список с сервера не пришёл — те же вопросы, что в демо на главной. */
const FALLBACK_QUESTIONS = SCENARIOS.map((item) => item.question);

interface Turn {
  id: number;
  question: string;
  /** Текст, пока модель пишет; итог с источниками — answer. */
  text: string;
  answer?: Answer;
  error?: string;
}

/**
 * Песочница (ТЗ §1): свой вопрос — ответ настоящего kronto по документам
 * вымышленной компании (src/corp_ed/demo). Без входа и без истории:
 * каждый вопрос — сам по себе, на сервере диалог не хранится.
 */
export function SandboxPage() {
  useSiteTitle(SITE.demo);
  const info = useQuery({
    queryKey: ["demo"],
    queryFn: () => unwrap(api.GET("/api/v1/demo")),
    retry: false,
    staleTime: Infinity,
  });
  const [turns, setTurns] = useState<Turn[]>([]);
  const [draft, setDraft] = useState("");
  const [tooShort, setTooShort] = useState(false);
  const nextId = useRef(1);
  const inputId = useId();
  const [pending, setPending] = useState(false);
  const stream = useRef<AbortController | null>(null);
  // Ушли со страницы — ответ дальше не читаем (сервер его тоже прервёт).
  useEffect(() => () => stream.current?.abort(), []);

  const off = info.error instanceof ApiError && info.error.code === "demo_off";
  const questions = info.data?.questions ?? FALLBACK_QUESTIONS;

  function update(id: number, change: (turn: Turn) => Turn) {
    setTurns((current) => current.map((turn) => (turn.id === id ? change(turn) : turn)));
  }

  /** Ответ печатается по мере генерации, как в чате; источники — в конце. */
  async function ask(id: number, question: string) {
    const controller = new AbortController();
    stream.current = controller;
    setPending(true);
    try {
      const body = await unwrap(
        api.POST("/api/v1/demo/ask/stream", {
          body: { question, website: "" },
          parseAs: "stream",
          signal: controller.signal,
        }),
      );
      if (!body) throw new Error("empty stream");
      let finished = false;
      for await (const event of readEvents<DemoEvent>(body)) {
        if (event.type === "delta")
          update(id, (turn) => ({ ...turn, text: turn.text + event.text }));
        else if (event.type === "reset") update(id, (turn) => ({ ...turn, text: "" }));
        else if (event.type === "done") {
          finished = true;
          update(id, (turn) => ({ ...turn, answer: event.answer }));
        } else if (event.type === "error") {
          finished = true;
          update(id, (turn) => ({ ...turn, error: event.message }));
        }
      }
      if (!finished) throw new Error("stream ended early");
    } catch (error) {
      if (!controller.signal.aborted) update(id, (turn) => ({ ...turn, error: askError(error) }));
    } finally {
      if (stream.current === controller) {
        stream.current = null;
        setPending(false);
      }
    }
  }

  function send(text: string) {
    const question = text.trim().replace(/\s+/g, " ");
    if (question.length < MIN_QUESTION) {
      setTooShort(true);
      return;
    }
    if (pending || question.length > MAX_QUESTION) return;
    setTooShort(false);
    const id = nextId.current++;
    setTurns((current) => [...current, { id, question, text: "" }]);
    setDraft("");
    void ask(id, question);
  }

  function submit(event: SubmitEvent) {
    event.preventDefault();
    send(draft);
  }

  return (
    <SiteLayout>
      <div className={`${site.container} ${site.section}`}>
        <SectionHead
          level={1}
          eyebrow="песочница"
          title="Спросите kronto сами"
          lead={`Ассистент ответит по документам вымышленной компании ${DEMO_COMPANY} и покажет, откуда взял ответ. Отвечает только по документам: на вопрос не о них честно скажет, что ответа нет.`}
        />

        {off ? (
          <Notice kind="info" title="Песочница сейчас недоступна">
            Попробуйте позже — или посмотрите <Link to="/#demo">пример на главной</Link>.
          </Notice>
        ) : null}

        <div className={styles.sandbox}>
          <aside className={styles.docs} aria-labelledby="sandbox-docs">
            <p className={styles.company}>{info.data?.company ?? DEMO_COMPANY}</p>
            <h2 id="sandbox-docs" className={styles.docsTitle}>
              Документы компании
            </h2>
            {info.data ? (
              <ul className={styles.docList}>
                {info.data.documents.map((title) => (
                  <li key={title}>
                    <FileText size={16} aria-hidden className={styles.docIcon} />
                    {title}
                  </li>
                ))}
              </ul>
            ) : info.isPending ? (
              <div className={styles.docSkeleton} role="status" aria-label="Загрузка">
                {[70, 55, 80, 60, 75].map((width) => (
                  <Skeleton key={width} width={`${width}%`} />
                ))}
              </div>
            ) : null}
            <p className={styles.docsNote}>
              Компания и документы вымышленные. В kronto вашей компании здесь будут ваши регламенты
              и инструкции.
            </p>
          </aside>

          <section className={styles.chat} aria-label="Вопросы и ответы">
            {turns.length === 0 ? (
              <div className={styles.empty}>
                <p className={styles.emptyTitle}>Выберите вопрос или напишите свой</p>
                <div className={styles.chips}>
                  {questions.map((question) => (
                    <button
                      key={question}
                      type="button"
                      className={styles.chip}
                      disabled={pending || off}
                      onClick={() => send(question)}
                    >
                      {question}
                    </button>
                  ))}
                </div>
              </div>
            ) : (
              <ol className={styles.thread} aria-live="polite">
                {turns.map((turn) => (
                  <li key={turn.id} className={styles.turn}>
                    <p className={styles.question}>
                      <span className="visually-hidden">Вопрос: </span>
                      {turn.question}
                    </p>
                    <TurnAnswer turn={turn} />
                  </li>
                ))}
              </ol>
            )}

            <form className={styles.composer} onSubmit={submit} noValidate>
              <label htmlFor={inputId} className="visually-hidden">
                Ваш вопрос
              </label>
              <textarea
                id={inputId}
                className={styles.input}
                rows={2}
                maxLength={MAX_QUESTION}
                placeholder="Например: сколько дней на авансовый отчёт?"
                value={draft}
                disabled={off}
                aria-invalid={tooShort || undefined}
                aria-describedby={`${inputId}-hint`}
                onChange={(e) => {
                  setDraft(e.target.value);
                  if (tooShort) setTooShort(false);
                }}
                onKeyDown={(e) => {
                  if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
                    e.preventDefault();
                    send(draft);
                  }
                }}
              />
              <button
                type="submit"
                className={styles.send}
                aria-label="Спросить"
                disabled={pending || off}
              >
                {pending ? <Spinner size={16} /> : <ArrowUp size={18} aria-hidden />}
              </button>
              <p id={`${inputId}-hint`} className={styles.hint}>
                {tooShort ? (
                  <span role="alert">Напишите вопрос — хотя бы пару слов.</span>
                ) : (
                  <>
                    До {MAX_QUESTION} символов, 10 вопросов в час. Не пишите персональные данные:
                    вопросы сохраняются в журнале.
                  </>
                )}
                <span className={styles.counter}>
                  {draft.length}/{MAX_QUESTION}
                </span>
              </p>
            </form>
          </section>
        </div>

        <div className={`${site.cta} ${styles.cta}`}>
          <h2>Ответы по документам вашей компании</h2>
          <p>
            Покажем kronto на ваших регламентах и инструкциях: подключим источники вместе с вами на
            созвоне. Внедрение бесплатно.
          </p>
          <div className={site.actions}>
            <Link to="/pricing/request" className={buttonClass("dark")}>
              Записаться на созвон
            </Link>
            <Link to="/pricing" className={buttonClass("ghost")}>
              Тарифы
            </Link>
          </div>
        </div>
      </div>
    </SiteLayout>
  );
}

function askError(error: unknown): string {
  if (error instanceof ApiError && error.status === 429) {
    return "Вопросов из песочницы на этот час больше нет. Попробуйте позже — или запишитесь на созвон, и мы покажем kronto на ваших документах.";
  }
  if (error instanceof ApiError && error.status === 422) {
    return `Вопрос — от ${MIN_QUESTION} до ${MAX_QUESTION} символов.`;
  }
  return errorMessage(error);
}

function TurnAnswer({ turn }: { turn: Turn }) {
  const [open, setOpen] = useState<number | null>(null);
  if (turn.error) {
    return (
      <div className={`${styles.answer} ${styles.failed}`} role="alert">
        {turn.error}
      </div>
    );
  }
  const answer = turn.answer;
  if (!answer && turn.text) {
    // Ссылки [n] — кнопками в итоге, когда придут источники.
    return (
      <div className={styles.answer} aria-busy="true">
        <Markdown renderCitation={() => null}>{turn.text}</Markdown>
      </div>
    );
  }
  if (!answer) {
    return (
      <p className={styles.searching}>
        <Spinner size={14} /> ищу в документах
      </p>
    );
  }
  if (answer.origin === "none") {
    return (
      <div className={`${styles.answer} ${styles.refusal}`}>
        <p className={styles.refusalTitle}>В документах компании нет ответа на этот вопрос.</p>
        <p>
          kronto не придумывает ответ, если его нет в документах. В вашей компании такие вопросы
          попадут в отчёт о пробелах — видно, каких документов не хватает.
        </p>
      </div>
    );
  }
  const sources = answer.sources;
  return (
    <div className={styles.answer}>
      <Markdown
        renderCitation={(n) =>
          sources[n - 1] ? (
            <button
              type="button"
              className={styles.cite}
              aria-pressed={open === n - 1}
              aria-label={`Источник ${n}`}
              onClick={() => setOpen(open === n - 1 ? null : n - 1)}
            >
              {n}
            </button>
          ) : null
        }
      >
        {answer.content}
      </Markdown>
      {sources.length ? (
        <ol className={styles.sources} aria-label="Источники">
          {sources.map((source, index) => {
            const expanded = open === index;
            return (
              <li key={index} className={styles.source}>
                <button
                  type="button"
                  className={styles.sourceToggle}
                  aria-expanded={expanded}
                  onClick={() => setOpen(expanded ? null : index)}
                >
                  <span className={`mono ${styles.sourceNumber}`}>{index + 1}</span>
                  <span>{[source.title, ...source.heading_path].join(" › ")}</span>
                </button>
                {expanded ? <p className={styles.sourceText}>{source.content}</p> : null}
              </li>
            );
          })}
        </ol>
      ) : null}
    </div>
  );
}
