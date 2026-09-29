/** Куда вернуться после входа: только свой путь, не внешний адрес. */
export function safeNext(value: string | null): string {
  if (!value || !value.startsWith("/") || value.startsWith("//")) return "/";
  return value;
}
