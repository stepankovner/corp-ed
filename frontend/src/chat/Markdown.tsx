import type { ComponentProps, ReactNode } from "react";
import ReactMarkdown, { type Components } from "react-markdown";
import remarkGfm from "remark-gfm";

import styles from "./Chat.module.css";
import { remarkCitations } from "./citations";

type CiteProps = ComponentProps<"cite"> & {
  "data-n"?: number | string;
  "data-source"?: number | string;
};

interface Props {
  children: string;
  /**
   * Отрисовать маркер [n]; без него маркеры остаются текстом. source — номер
   * выдержки, на которую сослался ответ (с citationMap n — номер карточки).
   */
  renderCitation?: (n: number, source: number) => ReactNode;
  /** Номер [n] → номер карточки источника (citations.ts, CitationOptions). */
  citationMap?: (n: number) => number | undefined;
  className?: string;
}

const BASE: Components = {
  // Ссылки из ответа модели и документов — наружу, без передачи адреса.
  a: ({ href, children }) => (
    <a href={href} target="_blank" rel="noopener noreferrer">
      {children}
    </a>
  ),
  table: ({ children }) => (
    <div className={styles.tableWrap}>
      <table>{children}</table>
    </div>
  ),
};

/** Markdown без сырого HTML и картинок: ответ модели — недоверенный текст. */
export function Markdown({ children, renderCitation, citationMap, className }: Props) {
  const components: Components = renderCitation
    ? {
        ...BASE,
        cite: ({ "data-n": raw, "data-source": source }: CiteProps) =>
          renderCitation(Number(raw), Number(source ?? raw)),
      }
    : BASE;
  return (
    <div className={[styles.markdown, className].filter(Boolean).join(" ")}>
      <ReactMarkdown
        remarkPlugins={
          renderCitation ? [remarkGfm, [remarkCitations, { map: citationMap }]] : [remarkGfm]
        }
        components={components}
        disallowedElements={["img"]}
        unwrapDisallowed
      >
        {children}
      </ReactMarkdown>
    </div>
  );
}
