import { ExternalLink, X } from "lucide-react";
import { useEffect, useRef } from "react";

import { IconButton } from "../ui/IconButton";
import styles from "./Chat.module.css";
import { safeHttpUrl } from "../lib/url";
import { Markdown } from "./Markdown";
import { fragmentText, sourceSection, type Source } from "./sources";

interface Props {
  source: Source;
  number: number;
  onClose: () => void;
}

/** Фрагмент документа, на который опирается ответ. */
export function SourcePanel({ source, number, onClose }: Props) {
  const panel = useRef<HTMLDivElement>(null);
  const url = safeHttpUrl(source.source_url);
  const section = sourceSection(source);

  useEffect(() => {
    panel.current?.focus();
    function onKey(event: KeyboardEvent) {
      if (event.key === "Escape") onClose();
    }
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [onClose, source]);

  return (
    <>
      <div className={styles.backdrop} onClick={onClose} aria-hidden />
      <div
        ref={panel}
        className={styles.panel}
        role="dialog"
        aria-modal="false"
        aria-labelledby="source-title"
        tabIndex={-1}
        id="source-panel"
      >
        <div className={styles.panelHead}>
          <div>
            <p className="mono muted">источник {number}</p>
            <h2 className={styles.panelTitle} id="source-title">
              {source.title}
            </h2>
          </div>
          <IconButton label="Закрыть источник" onClick={onClose}>
            <X size={20} aria-hidden />
          </IconButton>
        </div>
        <p className={styles.panelSection}>{section || "Фрагмент документа"}</p>
        <div className={styles.panelText}>
          <Markdown className={styles.hlBlock}>{fragmentText(source)}</Markdown>
        </div>
        {url ? (
          <a className={styles.panelLink} href={url} target="_blank" rel="noopener noreferrer">
            Открыть документ в источнике <ExternalLink size={16} aria-hidden />
          </a>
        ) : null}
      </div>
    </>
  );
}
