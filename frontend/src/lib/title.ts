import { useLayoutEffect } from "react";

export const APP_NAME = "kronto";

export function documentTitle(title?: string | null): string {
  return title ? `${title} — ${APP_NAME}` : APP_NAME;
}

/**
 * Заголовок вкладки браузера: «Документы — kronto». Ставится в том же
 * коммите, что и страница (layout-эффект): с обычным эффектом страница
 * успевала появиться раньше заголовка — и тест, проверявший его сразу,
 * через раз видел прежний.
 */
export function useDocumentTitle(title?: string | null): void {
  useLayoutEffect(() => {
    document.title = documentTitle(title);
  }, [title]);
}
