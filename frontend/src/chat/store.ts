import { createContext, useContext } from "react";

import type { Attachment, Origin, Turn } from "./api";

/** Ключ хода в новом диалоге — до события start, пока id диалога неизвестен. */
export const NEW_KEY = "new";

/**
 * Ответ, который печатается прямо сейчас (ТЗ §6). Остальное — в кэше
 * карточки диалога; здесь только то, что меняется по кускам потока.
 */
export interface LiveAnswer {
  conversationId: string | null;
  /** Вопрос до события start: показываем сразу, не дожидаясь сервера. */
  question: string;
  attachments: Attachment[];
  /** Ветка, к которой добавляется ход: ответ-родитель или вопрос при повторе. */
  parentId: string | null;
  regenerate: boolean;
  answerId: string | null;
  text: string;
  stage: "searching" | "writing";
  origin: Origin | null;
  stopping: boolean;
}

export interface SendOptions {
  attachments?: Attachment[];
  /** Сервер завёл ход (start): новый диалог получил id. */
  onStarted?: (conversationId: string) => void;
}

export interface ChatApi {
  live: Record<string, LiveAnswer>;
  send: (turn: Turn, options?: SendOptions) => Promise<void>;
  stop: (conversationId: string) => void;
}

export const ChatContext = createContext<ChatApi | null>(null);

export function useChat(): ChatApi {
  const value = useContext(ChatContext);
  if (!value) throw new Error("useChat outside ChatProvider");
  return value;
}

const LEGACY_PREFIX = "kronto.chat.";

/**
 * Предложения подключить свой аккаунт, скрытые на экране чата (ChatPage,
 * ConnectBanner): id подключений компании — данные человека, а не
 * настройка браузера.
 */
export const HIDDEN_CONNECT_KEY = "kronto:connect-banner-hidden";

/**
 * Данные человека в браузере — при входе, выходе и смене компании.
 * До этапа 6 переписка жила во вкладке (sessionStorage); теперь диалоги
 * на сервере, а здесь стираются её остатки и скрытые предложения
 * подключить источник. Тема и вид панели — настройки устройства, их не
 * трогаем.
 */
export function clearChatHistory(): void {
  try {
    for (const key of Object.keys(sessionStorage)) {
      if (key.startsWith(LEGACY_PREFIX)) sessionStorage.removeItem(key);
    }
  } catch {
    // Нечего чистить.
  }
  try {
    localStorage.removeItem(HIDDEN_CONNECT_KEY);
  } catch {
    // Хранилище недоступно (приватный режим) — нечего чистить.
  }
}
