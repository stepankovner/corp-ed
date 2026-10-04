import { useSyncExternalStore } from "react";

/**
 * Установка kronto как приложения (ТЗ §11): Chrome, Edge и Яндекс Браузер
 * присылают событие beforeinstallprompt, когда сайт можно установить
 * (manifest.webmanifest). Событие сохраняем, а предложение показываем по
 * кнопке в меню учётки — не всплывающим окном при входе. Safari события не
 * шлёт: там «Поделиться» → «На экран „Домой“» (статья «Помощи» phone).
 */
interface InstallPromptEvent extends Event {
  prompt: () => Promise<void>;
  userChoice: Promise<{ outcome: "accepted" | "dismissed" }>;
}

let pending: InstallPromptEvent | null = null;
const listeners = new Set<() => void>();

function notify(): void {
  listeners.forEach((listener) => listener());
}

if (typeof window !== "undefined") {
  window.addEventListener("beforeinstallprompt", (event) => {
    event.preventDefault();
    pending = event as InstallPromptEvent;
    notify();
  });
  window.addEventListener("appinstalled", () => {
    pending = null;
    notify();
  });
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

/** Предложить установку; null — браузер её сейчас не предлагает. */
export function useInstallPrompt(): (() => Promise<void>) | null {
  const event = useSyncExternalStore(
    subscribe,
    () => pending,
    () => null,
  );
  if (!event) return null;
  return async () => {
    await event.prompt();
    await event.userChoice.catch(() => undefined);
    // Одно событие — одно предложение; следующее браузер пришлёт сам.
    pending = null;
    notify();
  };
}
