/**
 * Приглашение, открытое до входа (ТЗ §2). Человек без учётки уходит на
 * регистрацию или вход — ссылку или код держим в памяти страницы, а не в
 * адресе и не в хранилище браузера: секрет не должен попасть в историю,
 * журналы сервера, заголовок Referer и на диск. Весь путь — приглашение →
 * регистрация или вход → код из письма → /join — переходы роутера в одной
 * вкладке, без перезагрузки страницы, и память его переживает. Ссылку из
 * письма открывают в новой вкладке — туда и sessionStorage не доходил.
 * Обновили страницу посреди пути — ссылку придётся открыть ещё раз.
 *
 * Секрет стирается, как только использован (вступление в компанию), и сам
 * — через INVITE_TTL_MS: вкладку могли оставить открытой надолго.
 */

export const INVITE_TTL_MS = 30 * 60 * 1000;

// Прежние версии хранили секрет в sessionStorage вкладки: убрать остаток.
const LEGACY_KEY = "kronto.invite";
try {
  sessionStorage.removeItem(LEGACY_KEY);
} catch {
  // Хранилище недоступно — там и нечего убирать.
}

let pending: { secret: string; expiresAt: number } | null = null;

export function savePendingInvite(secret: string): void {
  pending = { secret, expiresAt: Date.now() + INVITE_TTL_MS };
}

export function pendingInvite(): string | null {
  if (pending && Date.now() >= pending.expiresAt) pending = null;
  return pending?.secret ?? null;
}

export function clearPendingInvite(): void {
  pending = null;
}

/**
 * Секрет из того, что вставил человек: ссылка …/join#<токен>, сам токен
 * или код вида K7QM-4XPA (регистр и дефисы — как угодно).
 */
export function inviteSecretFrom(input: string): string | null {
  const value = input.trim();
  if (!value) return null;
  const hash = value.indexOf("#");
  if (hash >= 0) {
    const token = value.slice(hash + 1).trim();
    return token || null;
  }
  return value;
}
