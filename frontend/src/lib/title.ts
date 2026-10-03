import { useEffect } from "react";

export const APP_NAME = "kronto";

export function documentTitle(title?: string | null): string {
  return title ? `${title} — ${APP_NAME}` : APP_NAME;
}

/** Заголовок вкладки браузера: «Документы — kronto». */
export function useDocumentTitle(title?: string | null): void {
  useEffect(() => {
    document.title = documentTitle(title);
  }, [title]);
}
