/**
 * Сессия вкладки. Refresh-токен лежит в httpOnly-cookie (RISKS №44):
 * скрипт страницы его не видит, и XSS не унесёт сессию на 14 дней.
 * Access-токен (15 мин) живёт только в памяти вкладки; после перезагрузки
 * вкладка получает новый через /auth/refresh — браузер сам приложит cookie.
 *
 * Бэкенд ротирует refresh при каждом обновлении, а повторное предъявление
 * старого — сигнал кражи: отзывается вся цепочка. Cookie общая для вкладок,
 * поэтому обновления идут под межвкладочной блокировкой (Web Locks): вторая
 * вкладка отправит refresh уже с новой cookie, полученной первой.
 *
 * В localStorage — только признак «здесь входили» (без токенов): без него
 * анонимный посетитель при каждом открытии слал бы заведомо пустой refresh.
 * Вход и выход в одной вкладке другие узнают через BroadcastChannel.
 */

export interface Session {
  accessToken: string;
  /** Момент истечения access-токена, мс эпохи. */
  expiresAt: number;
}

const SIGNED_IN_KEY = "kronto.signedIn";
type Listener = (session: Session | null) => void;
const listeners = new Set<Listener>();

let current: Session | null = null;

export function getSession(): Session | null {
  return current;
}

export function setSession(session: Session | null): void {
  current = session;
  try {
    if (session) localStorage.setItem(SIGNED_IN_KEY, "1");
    else localStorage.removeItem(SIGNED_IN_KEY);
  } catch {
    // Хранилище недоступно (приватный режим) — после перезагрузки войти заново.
  }
  for (const listener of listeners) listener(session);
}

/**
 * Забыть сессию только в этой вкладке, не трогая признак входа: другая
 * вкладка сообщила о входе или выходе и уже сама обновила хранилище.
 */
export function dropSession(): void {
  current = null;
  for (const listener of listeners) listener(null);
}

/** Входили ли в этом браузере: стоит ли пробовать восстановить сессию. */
export function hasSignedInBefore(): boolean {
  try {
    return localStorage.getItem(SIGNED_IN_KEY) === "1";
  } catch {
    return false;
  }
}

export function subscribeSession(listener: Listener): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

export function sessionFromTokens(tokens: { access_token: string; expires_in: number }): Session {
  return {
    accessToken: tokens.access_token,
    expiresAt: Date.now() + tokens.expires_in * 1000,
  };
}

/** Что одна вкладка сообщает остальным. */
export type SessionEvent = "signed-in" | "signed-out";

const channel =
  typeof BroadcastChannel === "undefined" ? null : new BroadcastChannel("kronto.session");

export function announce(event: SessionEvent): void {
  channel?.postMessage(event);
}

export function onAnnouncement(handler: (event: SessionEvent) => void): () => void {
  if (!channel) return () => undefined;
  const listener = (message: MessageEvent<unknown>) => {
    if (message.data === "signed-in" || message.data === "signed-out") handler(message.data);
  };
  channel.addEventListener("message", listener);
  return () => channel.removeEventListener("message", listener);
}

/** Выполнить fn под межвкладочной блокировкой; без Web Locks — внутри вкладки. */
let localChain: Promise<unknown> = Promise.resolve();
export function withRefreshLock<T>(fn: () => Promise<T>): Promise<T> {
  if (typeof navigator !== "undefined" && "locks" in navigator) {
    // Блокировка держится, пока не завершится промис из колбэка; тип в
    // lib.dom этого не отражает и оборачивает результат второй раз.
    return navigator.locks.request("kronto.refresh", fn) as unknown as Promise<T>;
  }
  const run = localChain.then(fn, fn);
  localChain = run.catch(() => undefined);
  return run;
}
