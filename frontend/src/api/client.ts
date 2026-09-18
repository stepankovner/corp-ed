import { ApiError } from "./ApiError";

const BASE_URL = import.meta.env.VITE_API_URL ?? "http://localhost:8000";

/**
 * Колбэк разлогинивания. Ставит его AuthProvider: клиент не знает,
 * что такое «сессия», но знает, что 401 означает её конец.
 */
let onUnauthorized: (() => void) | null = null;

export function setUnauthorizedHandler(handler: () => void): void {
  onUnauthorized = handler;
}

interface RequestOptions {
  method?: "GET" | "POST" | "PATCH";
  body?: unknown;
  token?: string | null;
}

interface ValidationItem {
  loc: (string | number)[];
  msg: string;
}

/** Поле → сообщение из 422-ответа FastAPI. */
function fieldErrorsFrom(detail: ValidationItem[]): Record<string, string> {
  const errors: Record<string, string> = {};
  for (const item of detail) {
    // loc = ["body", "role_title"]; нужно имя поля, а не источник.
    const field = item.loc.filter((part) => part !== "body").at(-1);
    if (typeof field === "string" && !(field in errors)) {
      errors[field] = item.msg;
    }
  }
  return errors;
}

function messageFrom(payload: unknown): string | null {
  if (payload && typeof payload === "object" && "detail" in payload) {
    const detail = (payload as { detail: unknown }).detail;
    if (typeof detail === "string") {
      return detail;
    }
  }
  return null;
}

/**
 * Превращает ответ с ошибкой в ApiError с текстом для человека.
 *
 * Пустого экрана без объяснения быть не должно, поэтому текст есть
 * для каждого статуса, включая неизвестные.
 */
function toApiError(status: number, payload: unknown): ApiError {
  const detail = messageFrom(payload);

  switch (status) {
    case 401:
      return new ApiError(status, detail ?? "Сессия истекла, войдите заново");
    case 403:
      return new ApiError(status, "Недостаточно прав для этого действия");
    case 404:
      return new ApiError(status, detail ?? "Не найдено");
    case 422: {
      const raw = (payload as { detail?: unknown } | null)?.detail;
      const items = Array.isArray(raw) ? (raw as ValidationItem[]) : [];
      return new ApiError(
        status,
        "Проверьте заполнение полей",
        fieldErrorsFrom(items),
      );
    }
    case 502:
      return new ApiError(
        status,
        "Сервис модели недоступен, попробуйте позже",
      );
    default:
      return new ApiError(
        status,
        detail ?? `Ошибка сервера (${status}). Попробуйте ещё раз.`,
      );
  }
}

export async function request<T>(
  path: string,
  options: RequestOptions = {},
): Promise<T> {
  const { method = "GET", body, token } = options;

  let response: Response;
  try {
    response = await fetch(`${BASE_URL}${path}`, {
      method,
      headers: {
        ...(body === undefined ? {} : { "Content-Type": "application/json" }),
        ...(token ? { Authorization: `Bearer ${token}` } : {}),
      },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
  } catch {
    // Сюда попадают недоступный бэкенд и заблокированный CORS-запрос:
    // в обоих случаях ответа нет вообще.
    throw new ApiError(
      0,
      "Не удалось связаться с сервером. Проверьте, что бэкенд запущен.",
    );
  }

  if (response.ok) {
    if (response.status === 204) {
      return undefined as T;
    }
    return (await response.json()) as T;
  }

  const payload = await response.json().catch(() => null);

  if (response.status === 401) {
    onUnauthorized?.();
  }

  throw toApiError(response.status, payload);
}
