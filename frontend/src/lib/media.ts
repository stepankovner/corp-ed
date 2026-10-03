import { useSyncExternalStore } from "react";

function query(media: string): MediaQueryList | null {
  return typeof window.matchMedia === "function" ? window.matchMedia(media) : null;
}

/** Совпадает ли медиазапрос сейчас; без matchMedia (тесты) — нет. */
export function useMediaQuery(media: string): boolean {
  return useSyncExternalStore(
    (onChange) => {
      const list = query(media);
      list?.addEventListener("change", onChange);
      return () => list?.removeEventListener("change", onChange);
    },
    () => query(media)?.matches ?? false,
  );
}

/** Граница телефона и планшета: уже — боковая панель уезжает в выдвижное меню. */
export const MOBILE_QUERY = "(max-width: 859.98px)";
