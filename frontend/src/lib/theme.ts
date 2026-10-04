import { useSyncExternalStore } from "react";

/**
 * Тема оформления: «Системная», «Светлая» или «Тёмная». Выбор хранится в
 * localStorage этого браузера; при «Системной» атрибута data-theme нет и
 * цвета выбирает медиазапрос в tokens.css. До первой отрисовки тему ставит
 * скрипт в index.html — он повторяет applyTheme и должен с ней совпадать
 * (theme.test.ts сверяет ключ и цвета).
 */
export type ThemePreference = "system" | "light" | "dark";
export type Theme = "light" | "dark";

export const THEME_KEY = "kronto.theme";
const DARK_QUERY = "(prefers-color-scheme: dark)";

/** Цвет полосы браузера на телефоне — фон рабочей области (--bg). */
export const THEME_COLOR: Record<Theme, string> = { light: "#ffffff", dark: "#1c1b19" };

export const THEME_OPTIONS: { value: ThemePreference; label: string }[] = [
  { value: "system", label: "Системная" },
  { value: "light", label: "Светлая" },
  { value: "dark", label: "Тёмная" },
];

function isPreference(value: unknown): value is ThemePreference {
  return value === "system" || value === "light" || value === "dark";
}

export function readPreference(): ThemePreference {
  try {
    const value = localStorage.getItem(THEME_KEY);
    return isPreference(value) ? value : "system";
  } catch {
    // Хранилище недоступно (приватный режим, запрет cookie) — как в системе.
    return "system";
  }
}

function savePreference(preference: ThemePreference): void {
  try {
    if (preference === "system") localStorage.removeItem(THEME_KEY);
    else localStorage.setItem(THEME_KEY, preference);
  } catch {
    // Не сохранили — выбор продержится до перезагрузки.
  }
}

function darkQuery(): MediaQueryList | null {
  return typeof window.matchMedia === "function" ? window.matchMedia(DARK_QUERY) : null;
}

export function systemTheme(): Theme {
  return darkQuery()?.matches ? "dark" : "light";
}

export function resolveTheme(preference: ThemePreference, system: Theme = systemTheme()): Theme {
  return preference === "system" ? system : preference;
}

/** Атрибут темы на <html> и цвет полосы браузера. */
export function applyTheme(preference: ThemePreference): void {
  const root = document.documentElement;
  if (preference === "system") root.removeAttribute("data-theme");
  else root.setAttribute("data-theme", preference);
  document
    .querySelector('meta[name="theme-color"]')
    ?.setAttribute("content", THEME_COLOR[resolveTheme(preference)]);
}

interface Snapshot {
  preference: ThemePreference;
  theme: Theme;
}

let snapshot: Snapshot | null = null;
const listeners = new Set<() => void>();

function current(): Snapshot {
  if (!snapshot) {
    const preference = readPreference();
    snapshot = { preference, theme: resolveTheme(preference) };
  }
  return snapshot;
}

function refresh(preference: ThemePreference): void {
  const next = { preference, theme: resolveTheme(preference) };
  if (snapshot?.preference === next.preference && snapshot.theme === next.theme) return;
  snapshot = next;
  applyTheme(preference);
  listeners.forEach((listener) => listener());
}

export function setThemePreference(preference: ThemePreference): void {
  savePreference(preference);
  refresh(preference);
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  // Сменилась тема системы или выбор в соседней вкладке.
  const onSystem = () => refresh(current().preference);
  const onStorage = (event: StorageEvent) => {
    if (event.key === THEME_KEY || event.key === null) refresh(readPreference());
  };
  const query = darkQuery();
  query?.addEventListener("change", onSystem);
  window.addEventListener("storage", onStorage);
  return () => {
    listeners.delete(listener);
    query?.removeEventListener("change", onSystem);
    window.removeEventListener("storage", onStorage);
  };
}

/** Готовый HTML сайта (site/prerender.tsx) рисуется без браузера — как в системе. */
const SERVER_SNAPSHOT: Snapshot = { preference: "system", theme: "light" };

export function useTheme(): Snapshot & { setPreference: (value: ThemePreference) => void } {
  const value = useSyncExternalStore(subscribe, current, () => SERVER_SNAPSHOT);
  return { ...value, setPreference: setThemePreference };
}
