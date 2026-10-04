import createClient from "openapi-fetch";

import { ApiError, networkError, toApiError } from "./errors";
import type { components, paths } from "./schema";
import { getSession, sessionFromTokens, setSession, withRefreshLock } from "./session";

export type Schemas = components["schemas"];

const BASE = "/api/v1";
// Обновляем чуть раньше истечения: часы клиента и сервера расходятся.
const EXPIRY_MARGIN_MS = 30_000;
// Ручки без входа: токен к ним не прикладываем, а их 401 (неверный пароль)
// не повод обновлять сессию.
const PUBLIC_PATHS = new Set(
  [
    "/auth/login",
    "/auth/refresh",
    "/auth/mfa/verify",
    "/auth/mfa/resend",
    "/auth/mfa/passkey-options",
    "/auth/register",
    "/auth/verify-email",
    "/auth/verify-email/link",
    "/auth/verify-email/resend",
    "/auth/forgot-password",
    "/auth/reset-password",
    "/account/email/confirm",
    "/account/email/revert",
    "/invites/preview",
  ].map((path) => `${BASE}${path}`),
);

/**
 * Новый access-токен по refresh-cookie. failedToken — токен, с которым
 * запрос получил 401 (null — восстановление сессии после перезагрузки).
 */
async function refreshAccess(failedToken: string | null): Promise<string | null> {
  return withRefreshLock(async () => {
    const stored = getSession();
    // Пока ждали блокировку, параллельный запрос этой вкладки уже обновил токен.
    if (
      stored &&
      stored.accessToken !== failedToken &&
      stored.expiresAt - EXPIRY_MARGIN_MS > Date.now()
    ) {
      return stored.accessToken;
    }
    let response: Response;
    try {
      // Тела нет: refresh-токен браузер приложит сам из httpOnly-cookie.
      response = await fetch(`${BASE}/auth/refresh`, {
        method: "POST",
        credentials: "same-origin",
      });
    } catch {
      // Сеть — не повод выкидывать из сессии.
      throw networkError();
    }
    if (!response.ok) {
      if (response.status === 401 || response.status === 403) setSession(null);
      return null;
    }
    const tokens = (await response.json()) as Schemas["TokenResponse"];
    const session = sessionFromTokens(tokens);
    setSession(session);
    return session.accessToken;
  });
}

/** Восстановить сессию после перезагрузки страницы: true — вход есть. */
export async function restoreSession(): Promise<boolean> {
  return (await refreshAccess(null)) !== null;
}

async function currentAccess(): Promise<string | null> {
  const session = getSession();
  if (!session) return null;
  if (session.expiresAt - EXPIRY_MARGIN_MS > Date.now()) return session.accessToken;
  return refreshAccess(session.accessToken);
}

/**
 * fetch с токеном: подставляет access, при 401 один раз обновляет пару и
 * повторяет запрос. Тело запроса копируется заранее — повтор его не теряет.
 */
export async function authFetch(input: Request): Promise<Response> {
  const path = new URL(input.url, window.location.origin).pathname;
  if (PUBLIC_PATHS.has(path)) return fetch(input);
  const retry = input.clone();
  const token = await currentAccess();
  if (token) input.headers.set("Authorization", `Bearer ${token}`);
  const response = await fetch(input);
  if (response.status !== 401 || !token) return response;
  const fresh = await refreshAccess(token);
  if (!fresh) return response;
  retry.headers.set("Authorization", `Bearer ${fresh}`);
  return fetch(retry);
}

export const api = createClient<paths>({
  // Без window — предрендер сайта при сборке (site/prerender.tsx): запросов он не шлёт.
  baseUrl: typeof window === "undefined" ? "http://localhost" : window.location.origin,
  fetch: (request) => authFetch(request),
});

interface Result<T> {
  data?: T;
  error?: unknown;
  response: Response;
}

/** Ответ или ApiError: страницы работают с данными, а не с парой data/error. */
export async function unwrap<T>(pending: Promise<Result<T>>): Promise<T> {
  let result: Result<T>;
  try {
    result = await pending;
  } catch (error) {
    if (error instanceof ApiError) throw error;
    throw networkError();
  }
  if (!result.response.ok) throw toApiError(result.response.status, result.error);
  return result.data as T;
}
