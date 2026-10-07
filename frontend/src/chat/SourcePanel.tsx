import { ExternalLink, X } from "lucide-react";
import { useEffect, useRef } from "react";

import { useMediaQuery } from "../lib/media";
import { IconButton } from "../ui/IconButton";
import styles from "./Chat.module.css";
import { safeHttpUrl } from "../lib/url";
import { Markdown } from "./Markdown";
import { citedSources, fragmentText, sourceSection, type Source } from "./sources";

/** Как в Chat.module.css: уже — панель становится листом поверх затемнения. */
const SHEET_QUERY = "(max-width: 760px)";
const FOCUSABLE = 'a[href], button:not([disabled]), [tabindex]:not([tabindex="-1"])';

interface Props {
  /** Все источники ответа. */
  sources: readonly Source[];
  /** Текст ответа: по его ссылкам — какие фрагменты карточки показать. */
  content: string;
  /** Источник, который открыли (индекс с нуля). */
  index: number;
  onClose: () => void;
}

/**
 * Фрагменты, на которые опирается ответ: карточка целиком (раздел
 * документа или файл сотрудника — sources.ts, citedSources), только те, на
 * которые ответ ссылается; тот, который открыли, подсвечен.
 *
 * Диалог: фокус — в панель, Esc закрывает, фокус возвращается к ссылке,
 * которая её открыла. На широком экране панель сбоку и страница рядом
 * доступна — не модальная; на телефоне — лист поверх затемнения: модальная,
 * Tab не выходит из неё.
 */
export function SourcePanel({ sources, content, index, onClose }: Props) {
  const panel = useRef<HTMLDivElement>(null);
  const active = useRef<HTMLDivElement>(null);
  const opener = useRef<HTMLElement | null>(null);
  const modal = useMediaQuery(SHEET_QUERY);
  const group = citedSources(sources, content).cardOf(index);
  const source = sources[index];

  // Открыли (или открыли другой источник) — запоминаем, откуда, и фокус в панель.
  useEffect(() => {
    const current = document.activeElement;
    if (
      current instanceof HTMLElement &&
      current !== document.body &&
      !panel.current?.contains(current)
    ) {
      opener.current = current;
    }
    panel.current?.focus();
    active.current?.scrollIntoView({ block: "nearest" });
  }, [index, sources]);

  // Закрыли — фокус туда, откуда открыли, а не в начало страницы.
  useEffect(
    () => () => {
      if (opener.current?.isConnected) opener.current.focus();
    },
    [],
  );

  useEffect(() => {
    function onKey(event: KeyboardEvent) {
      if (event.key === "Escape") {
        onClose();
        return;
      }
      if (!modal || event.key !== "Tab" || !panel.current) return;
      const items = Array.from(panel.current.querySelectorAll<HTMLElement>(FOCUSABLE));
      const first = items[0];
      const last = items.at(-1);
      if (!first || !last) return;
      const focused = document.activeElement;
      if (event.shiftKey && (focused === first || focused === panel.current)) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && focused === last) {
        event.preventDefault();
        first.focus();
      }
    }
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [onClose, modal]);

  if (!group || !source) return null;
  const url = safeHttpUrl(source.source_url ?? null);
  const section = sourceSection(source);
  const several = group.numbers.length > 1;
  // Фрагменты из разных разделов (файл сотрудника) подписаны разделами.
  const sections = group.sources.map(sourceSection);
  const mixed = new Set(sections).size > 1;

  return (
    <>
      <div className={styles.backdrop} onClick={onClose} aria-hidden />
      <div
        ref={panel}
        className={styles.panel}
        role="dialog"
        aria-modal={modal}
        aria-labelledby="source-title"
        tabIndex={-1}
        id="source-panel"
      >
        <div className={styles.panelHead}>
          <div>
            <p className="mono muted">источник {group.display}</p>
            <h2 className={styles.panelTitle} id="source-title">
              {source.title}
            </h2>
          </div>
          <IconButton label="Закрыть источник" onClick={onClose}>
            <X size={20} aria-hidden />
          </IconButton>
        </div>
        <p className={styles.panelSection}>
          {mixed
            ? source.kind === "attachment"
              ? "Фрагменты вашего файла"
              : "Фрагменты документа"
            : section ||
              (source.kind === "attachment" ? "Фрагмент вашего файла" : "Фрагмент документа")}
        </p>
        {source.content === null ? (
          // Документ удалили или доступ к нему закрыли — фрагмент не показываем.
          <p className="muted">Документ удалён или вам недоступен: фрагмент не показываем.</p>
        ) : (
          group.sources.map((fragment, position) => {
            const number = group.numbers[position] ?? 0;
            const current = number === index + 1;
            return (
              <div
                key={number}
                ref={current ? active : undefined}
                className={`${styles.panelText} ${styles.fragment}`}
              >
                {several ? (
                  <p className="mono muted">
                    {(mixed && sections[position]) || `фрагмент ${position + 1}`}
                  </p>
                ) : null}
                <Markdown className={current ? styles.hlBlock : styles.fragmentBlock}>
                  {fragmentText(fragment)}
                </Markdown>
              </div>
            );
          })
        )}
        {url ? (
          <a className={styles.panelLink} href={url} target="_blank" rel="noopener noreferrer">
            Открыть документ в источнике <ExternalLink size={16} aria-hidden />
          </a>
        ) : null}
      </div>
    </>
  );
}
