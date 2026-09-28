const dateTime = new Intl.DateTimeFormat("ru-RU", {
  day: "numeric",
  month: "short",
  year: "numeric",
  hour: "2-digit",
  minute: "2-digit",
});
const dateOnly = new Intl.DateTimeFormat("ru-RU", {
  day: "numeric",
  month: "long",
  year: "numeric",
});
const relative = new Intl.RelativeTimeFormat("ru-RU", { numeric: "auto" });

export function formatDateTime(value: string | null | undefined): string {
  if (!value) return "—";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? "—" : dateTime.format(date);
}

export function formatDate(value: string | null | undefined): string {
  if (!value) return "—";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? "—" : dateOnly.format(date);
}

/** «5 минут назад», «вчера»; старше недели — дата. */
export function formatRelative(value: string | null | undefined, now = Date.now()): string {
  if (!value) return "—";
  const time = new Date(value).getTime();
  if (Number.isNaN(time)) return "—";
  const seconds = Math.round((time - now) / 1000);
  const abs = Math.abs(seconds);
  if (abs < 45) return "только что";
  if (abs < 3600) return relative.format(Math.round(seconds / 60), "minute");
  if (abs < 86400) return relative.format(Math.round(seconds / 3600), "hour");
  if (abs < 7 * 86400) return relative.format(Math.round(seconds / 86400), "day");
  return formatDate(value);
}

export function formatBytes(value: number | null | undefined): string {
  if (value == null) return "—";
  if (value < 1024) return `${value} Б`;
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(0)} КБ`;
  return `${(value / 1024 / 1024).toFixed(1).replace(".", ",")} МБ`;
}

export function formatNumber(value: number): string {
  return new Intl.NumberFormat("ru-RU").format(value);
}

/** 1 вопрос, 2 вопроса, 5 вопросов. */
export function plural(n: number, one: string, few: string, many: string): string {
  const mod10 = n % 10;
  const mod100 = n % 100;
  if (mod10 === 1 && mod100 !== 11) return one;
  if (mod10 >= 2 && mod10 <= 4 && (mod100 < 12 || mod100 > 14)) return few;
  return many;
}
