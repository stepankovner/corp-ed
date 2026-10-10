/**
 * Юридические тексты сайта (ТЗ §11) — проекты, которые проверяет юрист.
 *
 * Тексты лежат рядом в .md: это копии проектов без служебных пометок для
 * юриста. Сменился текст — новая версия и дата здесь; у регистрации и
 * записи на созвон ту же версию надо выставить на сервере
 * (REGISTRATION_POLICY_VERSION, REGISTRATION_TERMS_VERSION,
 * LEADS_POLICY_VERSION): согласие пишется в учётку и в заявку с версией.
 */
import consent from "./consent.md?raw";
import consentCall from "./consent-call.md?raw";
import cookies from "./cookies.md?raw";
import offer from "./offer.md?raw";
import privacy from "./privacy.md?raw";
import refund from "./refund.md?raw";
import terms from "./terms.md?raw";

/**
 * Реквизиты ИП — одно место на все документы и страницу «О компании».
 * Адрес — для писем (запросы и отзыв согласия по 152-ФЗ); не заполнен —
 * плейсхолдер в квадратных скобках, его видно на странице. Телефона нет:
 * связь — почта (решение владельца 10.10).
 */
export const REQUISITES = {
  fullName: "Ковнер Степан Анатольевич",
  inn: "272499012240",
  ogrnip: "326270000067321",
  address: "[адрес для корреспонденции]",
  account: "40802810820001208210",
  bank: "ООО «Банк Точка»",
  correspondentAccount: "30101810745374525104",
  bik: "044525104",
};

/** Плейсхолдер в тексте проекта → реквизит. */
const PLACEHOLDERS: [string, keyof typeof REQUISITES][] = [
  ["[ФИО]", "fullName"],
  ["[ИНН]", "inn"],
  ["[ОГРНИП]", "ogrnip"],
  ["[адрес для корреспонденции]", "address"],
  ["[р/с]", "account"],
  ["[наименование банка]", "bank"],
  ["[к/с]", "correspondentAccount"],
  ["[БИК]", "bik"],
];

export interface LegalDocument {
  /** Текст в Markdown, без заголовка — его рисует страница. */
  text: string;
  /** Дата редакции, ГГГГ-ММ-ДД. */
  edition: string;
  /** Версия, которую сервер пишет вместе с согласием; null — согласия нет. */
  version: string | null;
}

const EDITION = "2026-10-10";

export const LEGAL = {
  privacy: { text: privacy, edition: EDITION, version: null },
  terms: { text: terms, edition: EDITION, version: "terms-draft-2026-10-10" },
  consent: { text: consent, edition: EDITION, version: "draft-2026-10-10" },
  consentCall: { text: consentCall, edition: EDITION, version: "draft-2026-10-10" },
  offer: { text: offer, edition: EDITION, version: null },
  refund: { text: refund, edition: EDITION, version: null },
  cookies: { text: cookies, edition: EDITION, version: null },
} satisfies Record<string, LegalDocument>;

const editionFormat = new Intl.DateTimeFormat("ru-RU", {
  day: "numeric",
  month: "long",
  year: "numeric",
  timeZone: "UTC",
});

/** «9 октября 2026 г.» */
export function editionDate(edition: string): string {
  return editionFormat.format(new Date(`${edition}T00:00:00Z`));
}

/** Текст документа с реквизитами и датой редакции на месте плейсхолдеров. */
export function fillPlaceholders(doc: LegalDocument): string {
  let text = doc.text.replaceAll("[дата редакции]", editionDate(doc.edition));
  for (const [placeholder, key] of PLACEHOLDERS) {
    text = text.replaceAll(placeholder, REQUISITES[key]);
  }
  return text;
}
