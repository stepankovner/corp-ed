import { api, unwrap, type Schemas } from "../api/client";

/**
 * Оплата подписки и пакетов (решения владельца 09.10): слова для периодов,
 * способов и статусов, скачивание PDF. Суммы, скидки и цены — с бэкенда
 * (GET /billing), здесь их нет.
 */

export type Period = Schemas["SubscriptionChoiceRequest"]["period"];
export type Method = Schemas["SubscriptionChoiceRequest"]["payment_method"];
export type InvoiceStatus = Schemas["InvoiceResponse"]["status"];
export type SubscriptionStatus = Schemas["SubscriptionResponse"]["status"];
type Tone = "warn" | "ok" | "muted" | "error";

/** Оплата на странице тарифа — один ключ для подписки, счетов и актов. */
export const BILLING_KEY = ["billing"] as const;

export const PERIOD_TITLE: Record<Period, string> = {
  month: "Месяц",
  quarter: "Квартал",
  year: "Год",
};

/** «за месяц», «за квартал», «за год». */
export const PERIOD_FOR: Record<Period, string> = {
  month: "за месяц",
  quarter: "за квартал",
  year: "за год",
};

export const METHOD_TITLE: Record<Method, string> = {
  invoice: "Счёт для юрлица или ИП",
  card: "Картой или СБП",
};

export const INVOICE_STATUS: Record<InvoiceStatus, { label: string; tone: Tone }> = {
  awaiting_payment: { label: "Ждёт оплаты", tone: "warn" },
  paid: { label: "Оплачен", tone: "ok" },
  cancelled: { label: "Отменён", tone: "muted" },
};

export const SUBSCRIPTION_STATUS: Record<SubscriptionStatus, { label: string; tone: Tone }> = {
  active: { label: "Оплачена", tone: "ok" },
  awaiting_payment: { label: "Ждёт оплаты", tone: "warn" },
  overdue: { label: "Просрочена", tone: "error" },
  cancelled: { label: "Отключена", tone: "muted" },
};

export const INVOICE_KIND: Record<Schemas["InvoiceResponse"]["kind"], string> = {
  subscription: "Подписка",
  seats: "Доплата за места",
  credits: "Пакет кредитов",
};

/** PDF с токеном в заголовке: обычная ссылка его не несёт. */
function save(blob: Blob, filename: string): void {
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  document.body.append(link);
  link.click();
  link.remove();
  URL.revokeObjectURL(url);
}

export async function downloadInvoice(invoice: Schemas["InvoiceResponse"]): Promise<void> {
  const blob = await unwrap(
    api.GET("/api/v1/billing/invoices/{invoice_id}/pdf", {
      params: { path: { invoice_id: invoice.id } },
      parseAs: "blob",
    }),
  );
  save(blob, `${invoice.number}.pdf`);
}

export async function downloadAct(act: Schemas["ActResponse"]): Promise<void> {
  const blob = await unwrap(
    api.GET("/api/v1/billing/acts/{act_id}/pdf", {
      params: { path: { act_id: act.id } },
      parseAs: "blob",
    }),
  );
  save(blob, `act-${act.month.slice(0, 7)}.pdf`);
}

const MONTHS = [
  "январь",
  "февраль",
  "март",
  "апрель",
  "май",
  "июнь",
  "июль",
  "август",
  "сентябрь",
  "октябрь",
  "ноябрь",
  "декабрь",
];

/** «октябрь 2026» из «2026-10-01». */
export function monthName(value: string): string {
  const [year, month] = value.split("-").map(Number) as [number, number];
  return `${MONTHS[month - 1] ?? ""} ${year}`;
}
