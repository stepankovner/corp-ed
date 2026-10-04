import { ArrowRight, FileText, Globe } from "lucide-react";
import { useEffect, useState } from "react";
import { Link } from "react-router";

import { buttonClass } from "../ui/buttonClass";
import { WindowMark } from "../ui/Logo";
import { Spinner } from "../ui/Spinner";
import styles from "./DemoPreview.module.css";
import {
  DEMO_COMPANY,
  DEMO_DOCUMENTS,
  SCENARIOS,
  splitHighlights,
  splitMarkers,
  type AnswerBlock,
  type Scenario,
} from "./demoScenarios";

const SEARCH_MS = 700;

/**
 * Заготовленное демо на главной: четыре вопроса вымышленной компании и
 * ответы с источниками. Ничего не отправляет на сервер — свой вопрос
 * задают в песочнице (/demo), там отвечает настоящий kronto.
 */
export function DemoPreview() {
  const [scenarioId, setScenarioId] = useState(SCENARIOS[0]?.id ?? "");
  const [searching, setSearching] = useState(false);
  const [sourceIndex, setSourceIndex] = useState(0);
  const scenario = SCENARIOS.find((item) => item.id === scenarioId) ?? SCENARIOS[0];

  useEffect(() => {
    if (!searching) return;
    const timer = window.setTimeout(() => setSearching(false), SEARCH_MS);
    return () => window.clearTimeout(timer);
  }, [searching]);

  if (!scenario) return null;
  const source = scenario.sources?.[sourceIndex];
  const documents = Object.values(DEMO_DOCUMENTS);

  function choose(next: Scenario) {
    if (next.id === scenarioId) return;
    setScenarioId(next.id);
    setSourceIndex(0);
    setSearching(true);
  }

  return (
    <div className={styles.window}>
      <div className={styles.bar}>
        <WindowMark />
        <span className={styles.company}>{DEMO_COMPANY}</span>
        <span className={`mono ${styles.barNote}`}>пример на вымышленных документах</span>
      </div>
      <div className={styles.body}>
        <aside className={styles.docs} aria-label="Подключённые документы">
          <p className={styles.docsTitle}>
            Подключённые документы <span className="muted">{documents.length}</span>
          </p>
          <ul>
            {documents.map((doc) => (
              <li key={doc.title} className={styles.doc}>
                {doc.type === "web" ? (
                  <Globe size={16} aria-hidden className={styles.docIcon} />
                ) : (
                  <FileText size={16} aria-hidden className={styles.docIcon} />
                )}
                <span>{doc.title}</span>
                <span className={`mono ${styles.docPlace}`}>{doc.place}</span>
              </li>
            ))}
          </ul>
        </aside>

        <div className={styles.chat}>
          <div className={styles.questions} role="group" aria-label="Вопросы для примера">
            {SCENARIOS.map((item) => (
              <button
                key={item.id}
                type="button"
                className={styles.question}
                aria-pressed={item.id === scenario.id}
                onClick={() => choose(item)}
              >
                {item.question}
              </button>
            ))}
          </div>

          <div className={styles.thread} aria-live="polite">
            <p className={styles.user}>
              <span className="visually-hidden">Вопрос сотрудника: </span>
              {scenario.question}
            </p>
            {searching ? (
              <p className={styles.searching}>
                <Spinner size={14} /> ищу в документах
              </p>
            ) : scenario.refusal ? (
              <div className={`${styles.answer} ${styles.refusal}`}>
                <p className={styles.refusalTitle}>{scenario.refusal.title}</p>
                <p className="muted">{scenario.refusal.text}</p>
              </div>
            ) : (
              <div className={styles.answer}>
                <span className="visually-hidden">Ответ kronto: </span>
                {scenario.answer?.map((block, index) => (
                  <Block
                    key={index}
                    block={block}
                    active={sourceIndex + 1}
                    onSource={(n) => setSourceIndex(n - 1)}
                  />
                ))}
              </div>
            )}
          </div>

          {!searching && source ? (
            <figure className={styles.source}>
              <figcaption className={styles.sourceHead}>
                <span className={`mono ${styles.sourceNumber}`}>источник {sourceIndex + 1}</span>
                <span className={styles.sourceTitle}>{DEMO_DOCUMENTS[source.doc].title}</span>
                <span className={`mono ${styles.sourceRef}`}>
                  {source.section} · {source.ref}
                </span>
              </figcaption>
              {source.text.map((line) => (
                <p key={line} className={styles.sourceLine}>
                  {splitHighlights(line).map((part, index) =>
                    part.mark ? <mark key={index}>{part.text}</mark> : part.text,
                  )}
                </p>
              ))}
            </figure>
          ) : null}
        </div>
      </div>
      <div className={styles.foot}>
        <p className="muted">Ответы в этом примере заготовлены заранее.</p>
        <Link to="/demo" className={buttonClass("dark", "sm")}>
          Задать свой вопрос в песочнице <ArrowRight size={16} aria-hidden />
        </Link>
      </div>
    </div>
  );
}

function Block({
  block,
  active,
  onSource,
}: {
  block: AnswerBlock;
  active: number;
  onSource: (n: number) => void;
}) {
  if ("ol" in block) {
    return (
      <ol className={styles.list}>
        {block.ol.map((item) => (
          <li key={item}>
            <Marked text={item} active={active} onSource={onSource} />
          </li>
        ))}
      </ol>
    );
  }
  if ("file" in block) {
    return (
      <button
        type="button"
        className={styles.file}
        onClick={() => onSource(block.file.source)}
        aria-label={`Файл ${block.file.name}: показать источник`}
      >
        <FileText size={20} aria-hidden />
        <span>
          <span className={styles.fileName}>{block.file.name}</span>
          <span className={`mono ${styles.fileNote}`}>{block.file.note}</span>
        </span>
      </button>
    );
  }
  return (
    <p>
      <Marked text={block.p} active={active} onSource={onSource} />
    </p>
  );
}

function Marked({
  text,
  active,
  onSource,
}: {
  text: string;
  active: number;
  onSource: (n: number) => void;
}) {
  return (
    <>
      {splitMarkers(text).map((part, index) =>
        typeof part === "number" ? (
          <button
            key={index}
            type="button"
            className={styles.cite}
            aria-pressed={part === active}
            aria-label={`Источник ${part}`}
            onClick={() => onSource(part)}
          >
            {part}
          </button>
        ) : (
          part
        ),
      )}
    </>
  );
}
