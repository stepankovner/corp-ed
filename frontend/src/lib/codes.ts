/**
 * Коды ошибок бэкенда → текст для человека. Неизвестный код показываем
 * как есть: так его проще найти в журнале и в документации.
 */
const LABELS: Record<string, string> = {
  // Индексация документов
  embedding_provider_error: "Сервис векторизации недоступен, повторим позже",
  internal_error: "Внутренняя ошибка при обработке",
  material_not_found: "Документ удалён",
  document_too_large: "Документ слишком большой",
  unsupported_format: "Формат не поддерживается",
  corrupted: "Файл повреждён или не читается",
  timeout: "Разбор файла занял слишком много времени",
  empty: "В файле нет текста",
  encrypted: "Файл защищён паролем",
  // Подключения
  auth_failed: "Источник отклонил доступ — проверьте учётные данные",
  credentials_missing: "Не заданы учётные данные",
  credentials_unreadable: "Учётные данные не читаются — задайте их заново",
  kind_unknown: "Неизвестный тип подключения",
  source_unavailable: "Источник недоступен",
  budget_exhausted: "Исчерпан лимит на обработку документов",
  network_error: "Нет связи с источником",
  rate_limited: "Источник ограничил частоту запросов, повторим позже",
  forbidden: "Недостаточно прав в источнике",
  not_found: "Не найдено в источнике",
  unauthorized: "Источник не принял токен",
  invalid_grant: "Доступ отозван — подключитесь заново",
  refresh_token_missing: "Доступ истёк — подключитесь заново",
  app_credentials_missing: "Не заданы ключи приложения",
  org_id_missing: "Не указан ID организации",
  org_id_invalid: "ID организации — только цифры",
  wiki_login_required: "Откройте Яндекс Вики в браузере один раз, затем подключитесь снова",
  user_unknown: "Источник не сообщил, кто вы",
  invalid_config: "Настройки подключения неверны",
  connector_limit: "Достигнут лимит подключений",
  state_invalid: "Ссылка авторизации устарела — начните подключение заново",
  access_denied: "Вы отказались дать доступ",
  maintenance: "Источник на обслуживании, повторим позже",
};

export function describeCode(code: string | null | undefined): string {
  if (!code) return "";
  return LABELS[code] ?? code;
}
