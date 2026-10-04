/**
 * Приглашение, открытое до входа (ТЗ §2). Человек без учётки уходит на
 * регистрацию или вход — ссылку или код держим в sessionStorage вкладки,
 * а не в адресе: секрет не должен попасть в историю, журналы сервера и
 * заголовок Referer. После входа /join продолжит с того же приглашения.
 */

const KEY = "kronto.invite";

export function savePendingInvite(secret: string): void {
  try {
    sessionStorage.setItem(KEY, secret);
  } catch {
    // Хранилище недоступно — после входа ссылку придётся открыть ещё раз.
  }
}

export function pendingInvite(): string | null {
  try {
    return sessionStorage.getItem(KEY);
  } catch {
    return null;
  }
}

export function clearPendingInvite(): void {
  try {
    sessionStorage.removeItem(KEY);
  } catch {
    // Нечего чистить.
  }
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
