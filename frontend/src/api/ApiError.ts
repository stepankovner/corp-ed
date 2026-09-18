/** Ошибка запроса с готовым текстом для пользователя. */
export class ApiError extends Error {
  readonly status: number;
  /** Сообщения валидации по полям формы (ответ 422). */
  readonly fieldErrors: Record<string, string>;

  constructor(
    status: number,
    message: string,
    fieldErrors: Record<string, string> = {},
  ) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.fieldErrors = fieldErrors;
  }
}

/** Текст ошибки для показа: незнакомые исключения тоже не должны молчать. */
export function errorMessage(error: unknown): string {
  if (error instanceof ApiError) {
    return error.message;
  }
  if (error instanceof Error && error.message) {
    return error.message;
  }
  return "Что-то пошло не так. Попробуйте ещё раз.";
}

export function fieldErrorsOf(error: unknown): Record<string, string> {
  return error instanceof ApiError ? error.fieldErrors : {};
}

/** Ответ 403: экран открыт ролью, которой он не предназначен. */
export function isForbidden(error: unknown): boolean {
  return error instanceof ApiError && error.status === 403;
}
