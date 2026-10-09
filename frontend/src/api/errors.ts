/** Ошибка API в одном виде для всего интерфейса. */
export class ApiError extends Error {
  readonly status: number;
  readonly code: string | null;
  /** Поле формы, к которому относится ошибка (реквизиты: inn, kpp…). */
  readonly field: string | null;

  constructor(
    status: number,
    message: string,
    code: string | null = null,
    field: string | null = null,
  ) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
    this.field = field;
  }
}

const FALLBACK: Record<number, string> = {
  0: "Нет связи с сервером. Проверьте подключение и попробуйте ещё раз.",
  400: "Запрос не принят.",
  401: "Сессия истекла, войдите заново.",
  402: "Кредиты компании на этот месяц закончились.",
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
  let field: string | null = null;
  if (body && typeof body === "object") {
    const {
      detail,
      code: rawCode,
      field: rawField,
    } = body as { detail?: unknown; code?: unknown; field?: unknown };
    if (typeof detail === "string" && detail) message = detail;
    if (typeof rawCode === "string") code = rawCode;
    if (typeof rawField === "string") field = rawField;
  }
  return new ApiError(status, message, code, field);
}

export function networkError(): ApiError {
  return new ApiError(0, FALLBACK[0] ?? "Нет связи с сервером.");
}

export function errorMessage(error: unknown): string {
  if (error instanceof ApiError) return error.message;
  return networkError().message;
}
