import createClient from "openapi-fetch";

import { ApiError, networkError, toApiError } from "./errors";
import type { components, paths } from "./schema";
import {
  getSession,
  reloadSession,
  sessionFromTokens,
  setSession,
  withRefreshLock,
} from "./session";

export type Schemas = components["schemas"];

const BASE = "/api/v1";
// Обновляем чуть раньше истечения: часы клиента и сервера расходятся.
const EXPIRY_MARGIN_MS = 30_000;
const PUBLIC_PATHS = new Set([`${BASE}/auth/login`, `${BASE}/auth/refresh`]);

async function refreshAccess(failedToken: string | null): Promise<string | null> {
  return withRefreshLock(async () => {
    const stored = reloadSession();
    if (!stored) return null;
    // Другая вкладка уже обновила пару, пока мы ждали блокировку.
    if (stored.accessToken !== failedToken && stored.expiresAt - EXPIRY_MARGIN_MS > Date.now()) {
      return stored.accessToken;
    }
    let response: Response;
    try {
      response = await fetch(`${BASE}/auth/refresh`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ refresh_token: stored.refreshToken }),
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
  baseUrl: window.location.origin,
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
