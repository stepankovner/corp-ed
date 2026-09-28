/**
 * Токены сессии. Бэкенд выдаёт пару access (15 мин) + refresh (14 дней)
 * и при обновлении ротирует refresh: повторное использование старого —
 * сигнал кражи, бэкенд отзывает всю цепочку. Поэтому:
 * - пара хранится в localStorage одним ключом — все вкладки видят
 *   свежую пару, которую получила любая из них;
 * - обновление идёт под межвкладочной блокировкой (Web Locks), и внутри
 *   неё вкладка сначала перечитывает хранилище: если другая вкладка уже
 *   обновила пару, второй раз refresh не отправляется.
 * Перенос refresh в httpOnly-cookie — RISKS №44.
 */

export interface Session {
  accessToken: string;
  refreshToken: string;
  /** Момент истечения access-токена, мс эпохи. */
  expiresAt: number;
}

const KEY = "kronto.session";
type Listener = (session: Session | null) => void;
const listeners = new Set<Listener>();

function parse(raw: string | null): Session | null {
  if (!raw) return null;
  try {
    const value = JSON.parse(raw) as Partial<Session>;
    if (
      typeof value.accessToken === "string" &&
      typeof value.refreshToken === "string" &&
      typeof value.expiresAt === "number"
    ) {
      return value as Session;
    }
  } catch {
    // Повреждённое значение — как отсутствие сессии.
  }
  return null;
}

let current: Session | null = null;
try {
  current = parse(localStorage.getItem(KEY));
} catch {
  current = null;
}

export function getSession(): Session | null {
  return current;
}

export function setSession(session: Session | null): void {
  current = session;
  try {
    if (session) localStorage.setItem(KEY, JSON.stringify(session));
    else localStorage.removeItem(KEY);
  } catch {
    // Хранилище недоступно (приватный режим) — сессия живёт до перезагрузки.
  }
  for (const listener of listeners) listener(session);
}

/** Перечитать хранилище: другая вкладка могла обновить пару или выйти. */
export function reloadSession(): Session | null {
  try {
    current = parse(localStorage.getItem(KEY));
  } catch {
    // Остаётся то, что есть в памяти.
  }
  return current;
}

export function subscribeSession(listener: Listener): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

if (typeof window !== "undefined") {
  window.addEventListener("storage", (event) => {
    if (event.key !== KEY) return;
    current = parse(event.newValue);
    for (const listener of listeners) listener(current);
  });
}

export function sessionFromTokens(tokens: {
  access_token: string;
  refresh_token: string;
  expires_in: number;
}): Session {
  return {
    accessToken: tokens.access_token,
    refreshToken: tokens.refresh_token,
    expiresAt: Date.now() + tokens.expires_in * 1000,
  };
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
