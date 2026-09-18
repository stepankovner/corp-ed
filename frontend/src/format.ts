const DATE_FORMAT = new Intl.DateTimeFormat("ru-RU", {
  day: "2-digit",
  month: "2-digit",
  year: "numeric",
});

/** Дата из ISO-строки бэкенда: 18.09.2026. */
export function formatDate(iso: string): string {
  return DATE_FORMAT.format(new Date(iso));
}

/** Разделение числа на разряды: 4 820 / 20 000. */
export function formatCount(value: number): string {
  return value.toLocaleString("ru-RU");
}
