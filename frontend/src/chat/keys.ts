/** Ключи кэша чата: списки (с поиском) и карточки диалогов — под одним корнем. */
export const CONVERSATIONS_KEY = ["conversations"] as const;
export const CONVERSATION_LISTS_KEY = ["conversations", "list"] as const;
export const SUGGESTIONS_KEY = ["suggestions"] as const;

export function conversationListKey(query: string) {
  return [...CONVERSATION_LISTS_KEY, query] as const;
}

export function conversationKey(id: string) {
  return ["conversations", "item", id] as const;
}
