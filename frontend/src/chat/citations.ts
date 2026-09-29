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

function citation(n: number): Text {
  return {
    type: "text",
    value: String(n),
    data: { hName: "cite", hProperties: { dataN: n } },
  };
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

function walk(node: Root | (RootContent & WithChildren)): void {
  const out: RootContent[] = [];
  for (const child of node.children) {
    if (child.type === "text") {
      for (const part of splitCitations(child.value)) {
        out.push(typeof part === "number" ? citation(part) : { type: "text", value: part });
      }
      continue;
    }
    if (!SKIP.has(child.type) && hasChildren(child)) walk(child);
    out.push(child);
  }
  node.children = out;
}

export function remarkCitations() {
  return (tree: Root) => {
    walk(tree);
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
