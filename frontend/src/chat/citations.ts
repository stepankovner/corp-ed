import type { Root, RootContent, Text } from "mdast";

/**
 * Маркеры источников в ответе модели: [1], [2]; на случай отступления от
 * промпта — [1, 3] и [1–3]. Плагин remark превращает их в элементы
 * <cite data-n>, которые ответ рисует кнопками. Внутри кода и ссылок
 * маркеры не трогаем; номера пунктов вида [2.2] маркерами не считаются.
 */
const ITEM = String.raw`\d{1,2}(?:\s*[-–]\s*\d{1,2})?`;
const MARKER = new RegExp(String.raw`\[(${ITEM}(?:\s*,\s*${ITEM})*)\]`, "g");
const MAX_RANGE = 20;

function expand(item: string): number[] {
  const [from, to] = item.split(/[-–]/).map((part) => Number(part.trim()));
  if (from === undefined || Number.isNaN(from)) return [];
  if (to === undefined || Number.isNaN(to) || to < from || to - from >= MAX_RANGE) return [from];
  return Array.from({ length: to - from + 1 }, (_, index) => from + index);
}
const SKIP = new Set(["code", "inlineCode", "link", "linkReference", "html"]);

type WithChildren = { children: RootContent[] };

function hasChildren(node: RootContent | Root): node is RootContent & WithChildren {
  return "children" in node && Array.isArray(node.children);
}

/** n — номер, который видит сотрудник; source — номер выдержки [n] ответа. */
function citation(n: number, source: number = n): Text {
  return {
    type: "text",
    value: String(n),
    data: { hName: "cite", hProperties: { dataN: n, dataSource: source } },
  };
}

function citeSource(node: Text): number {
  const source = node.data?.hProperties?.dataSource;
  return typeof source === "number" ? source : Number(node.value);
}

export interface CitationOptions {
  /**
   * Номер [n] ответа → номер карточки источника. Задан — ссылки подряд на
   * одну карточку сливаются в один маркер, а из соседних фраз абзаца с
   * одной и той же ссылкой маркер остаётся только у последней (владелец
   * 06.10: ①②③④⑤⑥⑧⑨ к одной фразе). undefined — такого источника нет,
   * маркер остаётся текстом.
   */
  map?: (n: number) => number | undefined;
}

// Не предикат типа: маркер — тоже узел text, и «не маркер» не значит «не текст».
function isCite(node: RootContent | undefined): boolean {
  return node?.type === "text" && node.data?.hName === "cite";
}

/** Между маркерами одной группы — только пробелы и запятые. */
const SEPARATOR = /^[\s,;]*$/;

function isSeparator(node: RootContent | undefined): boolean {
  return node?.type === "text" && !isCite(node) && SEPARATOR.test(node.value);
}

interface Run {
  start: number;
  end: number;
  numbers: number[];
  /** Номер выдержки для каждой карточки группы — первый из сославшихся. */
  sources: number[];
}

function sameNumbers(a: number[], b: number[]): boolean {
  return a.length === b.length && a.every((n) => b.includes(n));
}

/** Маркеры подряд — группы; в группе каждая карточка по разу. */
function citationRuns(children: RootContent[]): Run[] {
  const runs: Run[] = [];
  let i = 0;
  while (i < children.length) {
    if (!isCite(children.at(i))) {
      i += 1;
      continue;
    }
    const run: Run = { start: i, end: i, numbers: [], sources: [] };
    let j = i;
    while (j < children.length) {
      if (isCite(children.at(j))) {
        run.end = j;
        j += 1;
      } else if (isSeparator(children.at(j)) && isCite(children.at(j + 1))) {
        j += 1;
      } else {
        break;
      }
    }
    for (const node of children.slice(run.start, run.end + 1)) {
      if (node.type !== "text" || !isCite(node)) continue;
      const n = Number(node.value);
      if (!run.numbers.includes(n)) {
        run.numbers.push(n);
        run.sources.push(citeSource(node));
      }
    }
    runs.push(run);
    i = run.end + 1;
  }
  return runs;
}

function collapse(children: RootContent[]): RootContent[] {
  const runs = citationRuns(children);
  const out: RootContent[] = [];
  let position = 0;
  runs.forEach((run, r) => {
    out.push(...children.slice(position, run.start));
    const next = runs.at(r + 1);
    if (next && sameNumbers(run.numbers, next.numbers)) {
      // Та же ссылка и у следующей фразы абзаца — маркер только там.
      const prev = out.at(-1);
      if (prev?.type === "text" && !isCite(prev)) prev.value = prev.value.trimEnd();
    } else {
      run.numbers.forEach((n, k) => out.push(citation(n, run.sources[k])));
    }
    position = run.end + 1;
  });
  out.push(...children.slice(position));
  return out;
}

export function splitCitations(value: string): (string | number)[] {
  const parts: (string | number)[] = [];
  let last = 0;
  for (const match of value.matchAll(MARKER)) {
    const index = match.index;
    if (index > last) parts.push(value.slice(last, index));
    for (const item of (match[1] ?? "").split(",")) parts.push(...expand(item));
    last = index + match[0].length;
  }
  if (last < value.length) parts.push(value.slice(last));
  return parts;
}

function walk(node: Root | (RootContent & WithChildren), map?: CitationOptions["map"]): void {
  const out: RootContent[] = [];
  for (const child of node.children) {
    if (child.type === "text") {
      for (const part of splitCitations(child.value)) {
        if (typeof part !== "number") {
          out.push({ type: "text", value: part });
          continue;
        }
        const shown = map ? map(part) : part;
        out.push(
          shown === undefined ? { type: "text", value: `[${part}]` } : citation(shown, part),
        );
      }
      continue;
    }
    if (!SKIP.has(child.type) && hasChildren(child)) walk(child, map);
    out.push(child);
  }
  node.children = map ? collapse(out) : out;
}

export function remarkCitations(options: CitationOptions = {}) {
  return (tree: Root) => {
    walk(tree, options.map);
  };
}

/** Номера источников, на которые ответ действительно ссылается. */
export function citedNumbers(content: string): Set<number> {
  const numbers = new Set<number>();
  for (const part of splitCitations(content)) if (typeof part === "number") numbers.add(part);
  return numbers;
}

/** Первая строка общего ответа — служебная пометка, её заменяет плашка. */
const GENERAL_PREFIX = /^\s*В документах компании ответа нет\.[^\n]*(?:\n+|$)/;

export function stripGeneralPrefix(content: string): string {
  return content.replace(GENERAL_PREFIX, "").trimStart();
}
