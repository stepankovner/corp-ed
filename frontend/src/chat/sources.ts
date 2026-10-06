import type { Source } from "./api";
import { citedNumbers } from "./citations";

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
 * Ключ карточки: фрагменты одного раздела документа (стенд 02.10); файл
 * сотрудника — одна карточка целиком: его разделы на карточке не видны,
 * и девять карточек «ваш файл» подряд выглядели одинаковыми (06.10).
 */
function groupKey(source: Groupable): string {
  if (source.kind === "attachment") {
    return JSON.stringify([source.kind, source.attachment_id ?? source.title]);
  }
  return JSON.stringify([
    source.kind ?? "",
    source.material_id ?? source.attachment_id ?? source.title,
    source.heading_path,
  ]);
}

/**
 * Карточки источников: фрагменты одного раздела документа — одна карточка
 * с номерами «1, 2», а не две одинаковые (стенд 02.10). Ссылки [n] в
 * тексте не меняются. Порядок — по первому номеру.
 */
export function groupSources<S extends Groupable>(sources: readonly S[]): SourceGroup<S>[] {
  const groups = new Map<string, SourceGroup<S>>();
  sources.forEach((source, index) => {
    const key = groupKey(source);
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

export interface SourceCard<S> extends SourceGroup<S> {
  /** Номер карточки в ответе: маркеры в тексте показывают его. */
  display: number;
}

export interface CitedSources<S> {
  /** Карточки, на которые ответ ссылается, — по порядку первой ссылки. */
  cards: SourceCard<S>[];
  /** Номер [n] ответа → номер его карточки; такого источника нет — undefined. */
  displayOf: (n: number) => number | undefined;
  /** Карточка источника с этим индексом (с нуля). */
  cardOf: (index: number) => SourceCard<S> | undefined;
}

/**
 * Источники ответа так, как их видит сотрудник (владелец 06.10): только
 * те, на которые ответ ссылается, по карточке на раздел документа или на
 * файл сотрудника, с номерами карточек 1, 2… по порядку первой ссылки.
 * Модель ссылается на выдержки, а их бывает девять из одного файла. Ответ
 * без ссылок — все источники, как раньше. API и текст ответа не меняются:
 * [n] по-прежнему номер выдержки.
 */
export function citedSources<S extends Groupable>(
  sources: readonly S[],
  content: string,
): CitedSources<S> {
  const order = [...citedNumbers(content)].filter((n) => n >= 1 && n <= sources.length);
  const shown = order.length > 0 ? order : sources.map((_, index) => index + 1);
  const rank = new Map(shown.map((n, position) => [n, position]));
  const cards: SourceCard<S>[] = [];
  for (const group of groupSources(sources)) {
    const kept = group.numbers.flatMap((n, position) => {
      const source = group.sources.at(position);
      return rank.has(n) && source ? [{ n, source }] : [];
    });
    const first = kept.at(0);
    if (!first) continue;
    cards.push({
      source: first.source,
      index: first.n - 1,
      numbers: kept.map((item) => item.n),
      sources: kept.map((item) => item.source),
      display: 0,
    });
  }
  const firstRank = (card: SourceCard<S>) =>
    Math.min(...card.numbers.map((n) => rank.get(n) ?? Number.MAX_SAFE_INTEGER));
  cards.sort((a, b) => firstRank(a) - firstRank(b));
  const byNumber = new Map<number, SourceCard<S>>();
  cards.forEach((card, position) => {
    card.display = position + 1;
    for (const n of card.numbers) byNumber.set(n, card);
  });
  return {
    cards,
    displayOf: (n) => byNumber.get(n)?.display,
    cardOf: (index) => byNumber.get(index + 1),
  };
}
