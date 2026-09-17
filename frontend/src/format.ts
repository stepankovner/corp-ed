const DATE_FORMAT = new Intl.DateTimeFormat("ru-RU", {
  day: "numeric",
  month: "long",
  hour: "2-digit",
  minute: "2-digit",
});

/** Дата из ISO-строки бэкенда в читаемом виде. */
export function formatDate(iso: string): string {
  return DATE_FORMAT.format(new Date(iso));
}

/** «3 фрагмента» — число с правильным окончанием. */
export function pluralize(
  count: number,
  one: string,
  few: string,
  many: string,
): string {
  const mod100 = count % 100;
  const mod10 = count % 10;

  if (mod100 >= 11 && mod100 <= 14) {
    return `${count} ${many}`;
  }
  if (mod10 === 1) {
    return `${count} ${one}`;
  }
  if (mod10 >= 2 && mod10 <= 4) {
    return `${count} ${few}`;
  }
  return `${count} ${many}`;
}
