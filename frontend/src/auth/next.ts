import { isOwnPath } from "../lib/url";

/** Куда вернуться после входа: только свой путь, не внешний адрес. */
export function safeNext(value: string | null): string {
  return value && isOwnPath(value) ? value : "/";
}
