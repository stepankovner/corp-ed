import type { Source } from "./api";

export type { Source };

/** Раздел документа: «Глава › Пункт». */
export function sourceSection(source: Pick<Source, "heading_path">): string {
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
export function fragmentText(source: Pick<Source, "title" | "heading_path" | "content">): string {
  const content = source.content ?? "";
  const crumbs = breadcrumbs(source.title, source.heading_path);
  const newline = content.indexOf("\n");
  if (crumbs && newline > 0 && content.slice(0, newline).trim() === crumbs) {
    return content.slice(newline + 1).trimStart();
  }
  return content;
}

interface Groupable {
  title: string;
  heading_path: string[];
  kind?: string;
  material_id?: string | null;
  attachment_id?: string | null;
}

export interface SourceGroup<S> {
  /** Первый фрагмент: заголовок и раздел карточки. */
  source: S;
  /** Его индекс (с нуля) — карточка открывает панель с него. */
  index: number;
  /** Номера источников — те же, что в ссылках [n] ответа. */
  numbers: number[];
  /** Все фрагменты раздела по порядку номеров. */
  sources: S[];
}

/**
 * Карточки источников: фрагменты одного раздела документа — одна карточка
 * с номерами «1, 2», а не две одинаковые (стенд 02.10). Ссылки [n] в
 * тексте не меняются. Порядок — по первому номеру.
 */
export function groupSources<S extends Groupable>(sources: readonly S[]): SourceGroup<S>[] {
  const groups = new Map<string, SourceGroup<S>>();
  sources.forEach((source, index) => {
    const key = JSON.stringify([
      source.kind ?? "",
      source.material_id ?? source.attachment_id ?? source.title,
      source.heading_path,
    ]);
    const group = groups.get(key);
    if (group) {
      group.numbers.push(index + 1);
      group.sources.push(source);
    } else {
      groups.set(key, { source, index, numbers: [index + 1], sources: [source] });
    }
  });
  return [...groups.values()];
}

/** Карточка, в которой источник с этим индексом (с нуля). */
export function sourceGroupOf<S extends Groupable>(
  sources: readonly S[],
  index: number,
): SourceGroup<S> | undefined {
  return groupSources(sources).find((group) => group.numbers.includes(index + 1));
}
