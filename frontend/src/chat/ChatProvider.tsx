import { useQueryClient } from "@tanstack/react-query";
import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react";

import {
  pathUpTo,
  stopAnswer,
  streamTurn,
  type Conversation,
  type Message,
  type Turn,
} from "./api";
import { CONVERSATION_LISTS_KEY, conversationKey } from "./keys";
import type { StreamEvent } from "./sse";
import { ChatContext, NEW_KEY, type ChatApi, type LiveAnswer, type SendOptions } from "./store";

type Start = Extract<StreamEvent, { type: "start" }>;

/** Карточка диалога после start: ветка до точки хода и новые сообщения. */
function applyStart(old: Conversation | undefined, event: Start, turn: Turn): Conversation {
  const messages = old?.messages ?? [];
  const next =
    turn.kind === "regenerate"
      ? [...pathUpTo(messages, turn.questionId), event.answer]
      : [
          ...(turn.kind === "ask" ? pathUpTo(messages, turn.parentId) : []),
          event.question,
          event.answer,
        ];
  return {
    share: null,
    ...old,
    ...event.conversation,
    current_message_id: event.answer.id,
    messages: next,
  };
}

function without<T>(record: Record<string, T>, key: string): Record<string, T> {
  return Object.fromEntries(Object.entries(record).filter(([name]) => name !== key));
}

/** Итог ответа: версии остаются из start — в done их нет. */
function applyAnswer(old: Conversation | undefined, answer: Message): Conversation | undefined {
  if (!old) return old;
  return {
    ...old,
    messages: old.messages.map((message) =>
      message.id === answer.id
        ? { ...answer, siblings: answer.siblings.length ? answer.siblings : message.siblings }
        : message,
    ),
  };
}

/**
 * Ответы, которые печатаются сейчас (ТЗ §6). Живёт в оболочке приложения:
 * переход в другой диалог поток не обрывает. Ответ пишет сервер; если
 * соединение оборвалось, он всё равно допишется — карточка диалога
 * опрашивается, пока ответ «пишется».
 */
export function ChatProvider({ children }: { children: ReactNode }) {
  const queryClient = useQueryClient();
  const [live, setLive] = useState<Record<string, LiveAnswer>>({});
  const liveRef = useRef(live);
  useEffect(() => {
    liveRef.current = live;
  }, [live]);
  const controllers = useRef(new Map<string, AbortController>());

  // Выход или другая компания — провайдер пересоздаётся: потоки закрыть.
  useEffect(() => {
    const open = controllers.current;
    return () => {
      for (const controller of open.values()) controller.abort();
    };
  }, []);

  const patch = useCallback((key: string, change: (item: LiveAnswer) => LiveAnswer) => {
    setLive((prev) => {
      const item = prev[key];
      return item ? { ...prev, [key]: change(item) } : prev;
    });
  }, []);

  const drop = useCallback((key: string) => {
    setLive((prev) => (key in prev ? without(prev, key) : prev));
  }, []);

  const send = useCallback(
    async (turn: Turn, options: SendOptions = {}) => {
      let key = turn.kind === "new" ? NEW_KEY : turn.conversationId;
      const controller = new AbortController();
      controllers.current.set(key, controller);
      setLive((prev) => ({
        ...prev,
        [key]: {
          conversationId: turn.kind === "new" ? null : turn.conversationId,
          question: turn.kind === "regenerate" ? "" : turn.question,
          attachments: options.attachments ?? [],
          parentId:
            turn.kind === "ask"
              ? turn.parentId
              : turn.kind === "regenerate"
                ? turn.questionId
                : null,
          regenerate: turn.kind === "regenerate",
          answerId: null,
          text: "",
          stage: "searching",
          origin: null,
          stopping: false,
        },
      }));

      const progress = { started: false };
      const onEvent = (event: StreamEvent) => {
        switch (event.type) {
          case "start": {
            progress.started = true;
            const id = event.conversation.id;
            if (key !== id) {
              const from = key;
              setLive((prev) => {
                const item = prev[from];
                const next = without(prev, from);
                if (item) next[id] = { ...item, conversationId: id, answerId: event.answer.id };
                return next;
              });
              controllers.current.delete(from);
              controllers.current.set(id, controller);
              key = id;
            } else {
              patch(key, (item) => ({ ...item, answerId: event.answer.id }));
            }
            queryClient.setQueryData<Conversation>(conversationKey(id), (old) =>
              applyStart(old, event, turn),
            );
            void queryClient.invalidateQueries({ queryKey: CONVERSATION_LISTS_KEY });
            options.onStarted?.(id);
            break;
          }
          case "stage":
            patch(key, (item) => ({ ...item, stage: event.stage }));
            break;
          case "origin":
            patch(key, (item) => ({ ...item, origin: event.origin }));
            break;
          case "delta":
            patch(key, (item) => ({ ...item, stage: "writing", text: item.text + event.text }));
            break;
          case "reset":
            patch(key, (item) => ({ ...item, text: "" }));
            break;
          case "done":
          case "error": {
            if (event.answer) {
              const answer = event.answer;
              queryClient.setQueryData<Conversation>(conversationKey(key), (old) =>
                applyAnswer(old, answer),
              );
            }
            drop(key);
            // Без итога (диалог удалили, пока писался ответ) — спросим сервер.
            if (!event.answer) {
              void queryClient.invalidateQueries({ queryKey: conversationKey(key) });
            }
            void queryClient.invalidateQueries({ queryKey: CONVERSATION_LISTS_KEY });
            break;
          }
        }
      };

      try {
        await streamTurn(turn, onEvent, controller.signal);
      } catch (error) {
        // До start — ошибка запроса (лимит, 409, 422): её покажет поле вопроса.
        if (!progress.started) throw error;
        // Поток оборвался — ответ допишется на сервере, карточку опросим.
        void queryClient.invalidateQueries({ queryKey: conversationKey(key) });
      } finally {
        drop(key);
        if (controllers.current.get(key) === controller) controllers.current.delete(key);
      }
    },
    [drop, patch, queryClient],
  );

  const stop = useCallback(
    (conversationId: string) => {
      const item = liveRef.current[conversationId];
      const controller = controllers.current.get(conversationId);
      if (!item?.answerId) {
        controller?.abort();
        return;
      }
      patch(conversationId, (current) => ({ ...current, stopping: true }));
      // Сервер сохранит то, что успело прийти, и закончит поток событием done.
      stopAnswer(conversationId, item.answerId).catch(() => controller?.abort());
    },
    [patch],
  );

  const value = useMemo<ChatApi>(() => ({ live, send, stop }), [live, send, stop]);
  return <ChatContext.Provider value={value}>{children}</ChatContext.Provider>;
}
