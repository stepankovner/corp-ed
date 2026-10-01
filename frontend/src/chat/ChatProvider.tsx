import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react";

import { api, unwrap } from "../api/client";
import { ApiError, networkError } from "../api/errors";
import {
  ChatContext,
  loadConversation,
  loadTurns,
  MAX_TURNS,
  PREFIX,
  saveConversation,
  type ChatApi,
  type Turn,
  type Vote,
} from "./store";

let counter = 0;
function newId(): string {
  counter += 1;
  return `${Date.now().toString(36)}-${counter}`;
}

export function ChatProvider({ userId, children }: { userId: string; children: ReactNode }) {
  const [turns, setTurns] = useState<Turn[]>(() => loadTurns(userId));
  // Диалог на бэкенде (BH-28): id приходит с первым ответом и уходит со
  // следующими вопросами. В ref — чтобы вопрос сразу после ответа не взял
  // устаревшее значение из замыкания.
  const conversation = useRef<string | null>(loadConversation(userId));

  useEffect(() => {
    try {
      const kept = turns.filter((turn) => turn.state !== "pending").slice(-MAX_TURNS);
      sessionStorage.setItem(PREFIX + userId, JSON.stringify(kept));
    } catch {
      // Без хранилища переписка просто не переживёт перезагрузку.
    }
  }, [turns, userId]);

  const patch = useCallback((id: string, next: Turn) => {
    setTurns((list) => list.map((turn) => (turn.id === id ? next : turn)));
  }, []);

  const run = useCallback(
    async (id: string, question: string) => {
      try {
        const body = conversation.current
          ? { question, conversation_id: conversation.current }
          : { question };
        const answer = await unwrap(api.POST("/api/v1/faq/ask", { body }));
        if (answer.conversation_id) {
          conversation.current = answer.conversation_id;
          saveConversation(userId, answer.conversation_id);
        }
        patch(id, { id, question, state: "done", answer, vote: null });
      } catch (error) {
        const failure = error instanceof ApiError ? error : networkError();
        patch(id, {
          id,
          question,
          state: "error",
          status: failure.status,
          code: failure.code,
          message: failure.message,
        });
      }
    },
    [patch, userId],
  );

  const ask = useCallback(
    async (question: string) => {
      const id = newId();
      setTurns((list) => [...list.slice(-(MAX_TURNS - 1)), { id, question, state: "pending" }]);
      await run(id, question);
    },
    [run],
  );

  const retry = useCallback(
    async (turnId: string) => {
      const turn = turns.find((item) => item.id === turnId);
      if (!turn) return;
      patch(turnId, { id: turnId, question: turn.question, state: "pending" });
      await run(turnId, turn.question);
    },
    [patch, run, turns],
  );

  const vote = useCallback(
    async (turnId: string, value: Vote) => {
      const turn = turns.find((item) => item.id === turnId);
      if (turn?.state !== "done" || !turn.answer.answer_id) return;
      const previous = turn.vote;
      patch(turnId, { ...turn, vote: value });
      try {
        await unwrap(
          api.PATCH("/api/v1/faq/answers/{answer_id}", {
            params: { path: { answer_id: turn.answer.answer_id } },
            body: { value },
          }),
        );
      } catch {
        patch(turnId, { ...turn, vote: previous });
      }
    },
    [patch, turns],
  );

  const reset = useCallback(() => {
    // «Новый диалог»: следующий вопрос — без истории на бэкенде.
    conversation.current = null;
    saveConversation(userId, null);
    setTurns([]);
  }, [userId]);
  const busy = turns.some((turn) => turn.state === "pending");

  const value = useMemo<ChatApi>(
    () => ({ turns, busy, ask, retry, vote, reset }),
    [turns, busy, ask, retry, vote, reset],
  );
  return <ChatContext.Provider value={value}>{children}</ChatContext.Provider>;
}
