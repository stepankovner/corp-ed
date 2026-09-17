const DATE_FORMAT = new Intl.DateTimeFormat("ru-RU", {
  day: "2-digit",
  month: "2-digit",
  year: "2-digit",
});

/** Дата из ISO-строки бэкенда: коротко, одинаковой ширины в колонке. */
export function formatDate(iso: string): string {
  return DATE_FORMAT.format(new Date(iso));
}
