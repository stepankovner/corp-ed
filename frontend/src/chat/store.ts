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
 * До этапа 6 переписка жила во вкладке (sessionStorage). Теперь диалоги на
 * сервере; остатки старой переписки стираем при входе и выходе.
 */
export function clearChatHistory(): void {
  try {
    for (const key of Object.keys(sessionStorage)) {
      if (key.startsWith(LEGACY_PREFIX)) sessionStorage.removeItem(key);
    }
  } catch {
    // Нечего чистить.
  }
}
