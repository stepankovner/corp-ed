import type { Schemas } from "../api/client";

export type Source = Schemas["FaqSourceResponse"];

/** Раздел документа: «Глава › Пункт». */
export function sourceSection(source: Source): string {
  return source.heading_path.filter(Boolean).join(" › ");
}

// Как domain/split.py: format_breadcrumbs.
const FILE_EXTENSION = /\.(?:docx?|pdf|txt|md|markdown|rtf|odt)$/i;

function breadcrumbs(title: string, headingPath: string[]): string {
  const parts: string[] = [];
  for (const raw of [title.trim().replace(FILE_EXTENSION, ""), ...headingPath]) {
    const part = raw.split(/\s+/).filter(Boolean).join(" ");
    const last = parts.at(-1);
    if (part && (last === undefined || part.toLowerCase() !== last.toLowerCase())) parts.push(part);
  }
  return parts.join(" > ");
}

/**
 * Текст фрагмента без служебной первой строки «Документ > Раздел»: её
 * бэкенд добавляет для модели, а панель показывает раздел отдельно.
 */
export function fragmentText(source: Source): string {
  const crumbs = breadcrumbs(source.title, source.heading_path);
  const newline = source.content.indexOf("\n");
  if (crumbs && newline > 0 && source.content.slice(0, newline).trim() === crumbs) {
    return source.content.slice(newline + 1).trimStart();
  }
  return source.content;
}
