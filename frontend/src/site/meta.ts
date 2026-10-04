import { useLayoutEffect } from "react";

import { documentTitle } from "../lib/title";

/**
 * Публичный сайт (ТЗ §1): адреса, заголовки и описания страниц. Отсюда
 * же их берёт предрендер (src/site/prerender.tsx): заголовок вкладки,
 * <meta name="description"> и канонический адрес в готовом HTML.
 */
export const SITE_URL = "https://krontoai.ru";

export const CONTACTS = {
  email: "info@krontoai.ru",
  telegram: "stukalich",
} as const;

export interface SiteMeta {
  path: string;
  /** Полный заголовок вкладки. */
  title: string;
  description: string;
}

function page(path: string, title: string, description: string): SiteMeta {
  return { path, title: documentTitle(title), description };
}

export const SITE = {
  home: {
    path: "/",
    title: "kronto — ИИ-ассистент по документам компании",
    description:
      "Сотрудники спрашивают обычными словами — kronto отвечает по регламентам и инструкциям компании со ссылкой на источник. Данные хранятся в России.",
  },
  demo: page(
    "/demo",
    "Песочница",
    "Задайте свой вопрос kronto: ассистент ответит по документам вымышленной строительной компании и покажет источник.",
  ),
  pricing: page(
    "/pricing",
    "Тарифы",
    "Тарифы kronto: «Базовый» — 990 ₽ и «Расширенный» — 1 290 ₽ за место в месяц, «Корпоративный» — по запросу. Внедрение бесплатно.",
  ),
  security: page(
    "/security",
    "Безопасность и данные",
    "Где хранятся документы, как изолированы компании, кто что видит и как защищён вход в kronto.",
  ),
  about: page("/about", "О компании", "Кто делает kronto, как с нами связаться и реквизиты."),
  help: page(
    "/help",
    "Помощь",
    "Короткие ответы о том, как устроен kronto: вопросы и ответы, документы, подключения, второй фактор.",
  ),
  privacy: page(
    "/privacy",
    "Политика обработки персональных данных",
    "Какие персональные данные обрабатывает kronto, зачем и как долго.",
  ),
  terms: page(
    "/terms",
    "Пользовательское соглашение",
    "Условия использования сервиса kronto: учётная запись, ответы ассистента, ограничения и оплата.",
  ),
  consent: page(
    "/consent",
    "Согласие на обработку персональных данных",
    "Текст согласия на обработку персональных данных при регистрации в kronto.",
  ),
} satisfies Record<string, SiteMeta>;

export type SitePageKey = keyof typeof SITE;

/** Заголовок вкладки страницы сайта — тот же, что в готовом HTML. */
export function useSiteTitle(meta: SiteMeta): void {
  useLayoutEffect(() => {
    document.title = meta.title;
  }, [meta.title]);
}
