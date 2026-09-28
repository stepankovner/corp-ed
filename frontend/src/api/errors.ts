/** Ошибка API в одном виде для всего интерфейса. */
export class ApiError extends Error {
  readonly status: number;
  readonly code: string | null;

  constructor(status: number, message: string, code: string | null = null) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
  }
}

const FALLBACK: Record<number, string> = {
  0: "Нет связи с сервером. Проверьте подключение и попробуйте ещё раз.",
  400: "Запрос не принят.",
  401: "Сессия истекла, войдите заново.",
  402: "Лимит вопросов на этот месяц исчерпан.",
  403: "Недостаточно прав.",
  404: "Не найдено.",
  409: "Конфликт с текущими данными.",
  413: "Файл слишком большой.",
  422: "Проверьте заполнение полей.",
  429: "Слишком много запросов. Подождите немного.",
  502: "Сервис языковой модели недоступен, попробуйте позже.",
  503: "Сервис временно недоступен, попробуйте позже.",
};

/** Из тела ответа FastAPI: {detail: строка | [{msg}], code?}. */
export function toApiError(status: number, body: unknown): ApiError {
  let message = FALLBACK[status] ?? "Что-то пошло не так. Попробуйте ещё раз.";
  let code: string | null = null;
  if (body && typeof body === "object") {
    const { detail, code: rawCode } = body as { detail?: unknown; code?: unknown };
    if (typeof detail === "string" && detail) message = detail;
    if (typeof rawCode === "string") code = rawCode;
  }
  return new ApiError(status, message, code);
}

export function networkError(): ApiError {
  return new ApiError(0, FALLBACK[0] ?? "Нет связи с сервером.");
}

export function errorMessage(error: unknown): string {
  if (error instanceof ApiError) return error.message;
  return networkError().message;
}
