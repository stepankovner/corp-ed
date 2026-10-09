import { formatNumber, plural } from "./format";

/**
 * Кредиты — единица расхода во всём интерфейсе (решение владельца 09.10):
 * не «вопросы» и не «обращения». Цены пакетов и средняя стоимость вопроса
 * приходят с бэкенда — здесь только слова и форматирование.
 */

/** «1 кредит», «3 кредита», «2 000 кредитов». */
export function credits(n: number): string {
  return `${formatNumber(n)} ${plural(n, "кредит", "кредита", "кредитов")}`;
}

/** «около 1 кредита», «около 1,5 кредита», «около 2 кредитов». */
export function aboutCredits(n: number): string {
  const word = Number.isInteger(n) ? plural(n, "кредита", "кредитов", "кредитов") : "кредита";
  return `около ${formatNumber(n)} ${word}`;
}

/** Пояснение рядом с кредитами: сколько в среднем стоит вопрос. */
export function averageQuestionNote(avg: number): string {
  return `В среднем один вопрос — ${aboutCredits(avg)}; длинные вопросы и ответы списывают больше.`;
}

/** «5 490 ₽», «1 490,50 ₽» — сумма в копейках, разряды неразрывным пробелом. */
export function formatKopecks(kopecks: number): string {
  const rubles = kopecks / 100;
  const text = new Intl.NumberFormat("ru-RU", {
    minimumFractionDigits: Number.isInteger(rubles) ? 0 : 2,
    maximumFractionDigits: 2,
  }).format(rubles);
  return `${text.replace(/\s/g, "\u00a0")}\u00a0₽`;
}
