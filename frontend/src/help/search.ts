import { Children, isValidElement, type ReactNode } from "react";

/** Текст разметки статьи: строки и числа из всех вложенных элементов. */
export function textOf(node: ReactNode): string {
  return Children.toArray(node)
    .map((child) => {
      if (typeof child === "string" || typeof child === "number") return String(child);
      if (isValidElement<{ children?: ReactNode }>(child)) return textOf(child.props.children);
      return "";
    })
    .join(" ");
}

/** Слова текста: нижний регистр, «ё» как «е», без знаков препинания. */
function words(text: string): string[] {
  return text
    .toLowerCase()
    .replace(/ё/g, "е")
    .split(/[^\p{L}\p{N}]+/u)
    .filter(Boolean);
}

/**
 * Текст для поиска: слова через пробел и пробел в начале — так слово
 * запроса ищется с начала слова текста («ключ» не найдёт «подключить»).
 */
export function searchable(text: string): string {
  return ` ${words(text).join(" ")} `;
}

const ENDING =
  /(?:ами|ями|ого|его|ому|ему|ыми|ими|ов|ев|ей|ий|ый|ой|ая|яя|ое|ее|ые|ие|ых|их|ым|им|ам|ям|ах|ях|ом|ем|ую|юю|ть)$/u;
const VOWELS = /[аеиоуыэюяьй]+$/u;

/**
 * Грубая основа слова, чтобы искать без точной формы: «файлы» найдут
 * «файл», «приглашения» — «приглашение», «пароли» — «пароль».
 */
export function stem(word: string): string {
  if (word.length < 5) return word;
  const cut = word.replace(ENDING, "").replace(VOWELS, "");
  return cut.length >= 3 ? cut : word;
}

/** Слова запроса — основами; пустой запрос — пустой список. */
export function terms(query: string): string[] {
  return words(query).map(stem);
}

/** Каждое слово запроса начинает какое-то слово текста (из searchable). */
export function matches(text: string, query: string): boolean {
  return terms(query).every((term) => text.includes(` ${term}`));
}
