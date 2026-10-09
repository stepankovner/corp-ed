/**
 * Токен общей ссылки на диалог (/shared#<токен>) в хранилище вкладки, а не
 * в адресе: фрагмент не уходит на сервер, а из адресной строки страница его
 * сразу убирает. Отсюда его берут вход (гость уходит на /login и
 * возвращается на /shared уже без фрагмента) и перезагрузка страницы.
 */

const KEY = "kronto.shared";

export function savePendingShare(token: string): void {
  try {
    sessionStorage.setItem(KEY, token);
  } catch {
    // Хранилище недоступно — после входа ссылку придётся открыть ещё раз.
  }
}

export function pendingShare(): string | null {
  try {
    return sessionStorage.getItem(KEY);
  } catch {
    return null;
  }
}
