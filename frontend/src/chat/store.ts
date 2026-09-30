import { createContext, useContext } from "react";

import type { Schemas } from "../api/client";

export type Answer = Schemas["FaqAnswerResponse"];
export type Vote = 1 | -1;

export type Turn =
  | { id: string; question: string; state: "pending" }
  | { id: string; question: string; state: "done"; answer: Answer; vote: Vote | null }
  | {
      id: string;
      question: string;
      state: "error";
      status: number;
      code: string | null;
      message: string;
    };

export interface ChatApi {
  turns: Turn[];
  busy: boolean;
  ask: (question: string) => Promise<void>;
  retry: (turnId: string) => Promise<void>;
  vote: (turnId: string, value: Vote) => Promise<void>;
  reset: () => void;
}

export const ChatContext = createContext<ChatApi | null>(null);
export const PREFIX = "kronto.chat.";
export const MAX_TURNS = 50;

export function useChat(): ChatApi {
  const value = useContext(ChatContext);
  if (!value) throw new Error("useChat outside ChatProvider");
  return value;
}

/**
 * Переписка живёт до закрытия вкладки. Бэкенд помнит последние реплики
 * диалога (conversation_id) несколько часов — для уточняющих вопросов;
 * показывает переписку только фронт.
 */
export function loadTurns(userId: string): Turn[] {
  try {
    const raw = sessionStorage.getItem(PREFIX + userId);
    const turns = raw ? (JSON.parse(raw) as Turn[]) : [];
    return Array.isArray(turns) ? turns.filter((turn) => turn.state !== "pending") : [];
  } catch {
    return [];
  }
}

/** Ключ диалога на бэкенде (BH-28): с ним уточняющий вопрос понимается в контексте. */
export function loadConversation(userId: string): string | null {
  try {
    return sessionStorage.getItem(PREFIX + userId + CONVERSATION);
  } catch {
    return null;
  }
}

export function saveConversation(userId: string, conversation: string | null): void {
  try {
    const key = PREFIX + userId + CONVERSATION;
    if (conversation) sessionStorage.setItem(key, conversation);
    else sessionStorage.removeItem(key);
  } catch {
    // Без хранилища новый диалог начнётся после перезагрузки.
  }
}

const CONVERSATION = ".conversation";

export function clearChatHistory(): void {
  try {
    for (const key of Object.keys(sessionStorage)) {
      if (key.startsWith(PREFIX)) sessionStorage.removeItem(key);
    }
  } catch {
    // Нечего чистить.
  }
}
