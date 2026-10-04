/** Инициалы человека: из имени, а без него — из почты. */
export function personInitials(name: string | null | undefined, email: string): string {
  const source = name?.trim() || email;
  const parts = source.split(/[\s.@_-]+/).filter(Boolean);
  const letters =
    parts.length > 1 ? (parts[0]?.[0] ?? "") + (parts[1]?.[0] ?? "") : source.slice(0, 2);
  return letters.toUpperCase();
}

// Организационно-правовая форма в начале названия инициалам не нужна.
const LEGAL_FORMS = /^(ооо|оао|зао|пао|ао|нао|ип|нко|ано|гк|ук|llc|ltd|inc)$/i;

/** Инициалы компании: «ООО «Меридиан Строй»» → «МС», «Ромашка» → «Ро». */
export function companyInitials(name: string): string {
  const words = name
    .replace(/[«»"'“”„()]/g, " ")
    .split(/[\s\-–—]+/)
    .filter((word) => word && !LEGAL_FORMS.test(word));
  const [first = "", second = ""] = words.length ? words : [name.trim()];
  if (!first) return "?";
  if (second) return ((first[0] ?? "") + (second[0] ?? "")).toUpperCase();
  // Одно слово: аббревиатуру оставляем как есть, иначе — «Ро».
  const pair = first.slice(0, 2);
  return pair === pair.toUpperCase() ? pair : (pair[0]?.toUpperCase() ?? "") + pair.slice(1);
}

/** Номер цвета плитки (1…count) — один и тот же для одного названия. */
export function tileIndex(name: string, count = 8): number {
  let hash = 0;
  for (const char of name.trim().toLowerCase()) {
    hash = (hash * 31 + (char.codePointAt(0) ?? 0)) >>> 0;
  }
  return (hash % count) + 1;
}
