const OWN_ORIGIN = "http://own.invalid";
/** Сколько раз раскодировать путь, прежде чем счесть его подозрительным. */
const MAX_DECODES = 4;

/** Обратный слэш браузер читает как «/», а таб и перевод строки молча выбрасывает. */
function hasUnsafeChar(value: string): boolean {
  for (const char of value) {
    const code = char.charCodeAt(0);
    if (code < 0x20 || code === 0x7f || char === "\\") return true;
  }
  return false;
}

/**
 * Путь на этом же сайте: «/…», не «//host» и не «/\host» — ни как есть,
 * ни после раскодирования (%2F, %5C, %252F…), ни после разбора «..».
 */
export function isOwnPath(value: string): boolean {
  let current = value;
  for (let step = 0; step < MAX_DECODES; step += 1) {
    if (!current.startsWith("/") || current.startsWith("//") || hasUnsafeChar(current)) {
      return false;
    }
    let url: URL;
    try {
      url = new URL(current, OWN_ORIGIN);
    } catch {
      return false;
    }
    if (url.origin !== OWN_ORIGIN || url.pathname.startsWith("//")) return false;
    let decoded: string;
    try {
      decoded = decodeURIComponent(current);
    } catch {
      // Битая кодировка в исходной строке — отказ; после раскодирования
      // «%25» — просто знак процента, дальше раскодировать нечего.
      return step > 0;
    }
    if (decoded === current) return true;
    current = decoded;
  }
  return false;
}

/** Ссылка на источник: только http(s) — javascript: и прочее не открываем. */
export function safeHttpUrl(value: string | null | undefined): string | null {
  if (!value) return null;
  try {
    const url = new URL(value);
    return url.protocol === "https:" || url.protocol === "http:" ? url.href : null;
  } catch {
    return null;
  }
}

/** Ссылка из ответа сервера: свой путь («/privacy») или http(s)-адрес. */
export function safeLinkHref(value: string | null | undefined): string | null {
  if (!value) return null;
  return isOwnPath(value) ? value : safeHttpUrl(value);
}
