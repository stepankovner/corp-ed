import type { FeedbackReason } from "./api";

/** «Что не так» у 👎: в форме под ответом и в обзоре админки. */
export const FEEDBACK_REASONS: { value: FeedbackReason; label: string }[] = [
  { value: "inaccurate", label: "Неточно или неверно" },
  { value: "incomplete", label: "Неполный ответ" },
  { value: "outdated", label: "Устаревшие сведения" },
  { value: "wrong_source", label: "Не тот документ" },
  { value: "other", label: "Другое" },
];

export function reasonLabel(value: string | null | undefined): string | null {
  if (!value) return null;
  return FEEDBACK_REASONS.find((item) => item.value === value)?.label ?? value;
}
