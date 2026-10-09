import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Link } from "react-router";

import { api, unwrap, type Schemas } from "../api/client";
import { errorMessage } from "../api/errors";
import {
  BILLING_KEY,
  downloadAct,
  downloadInvoice,
  INVOICE_KIND,
  INVOICE_STATUS,
  METHOD_TITLE,
  monthName,
  PERIOD_FOR,
  PERIOD_TITLE,
  SUBSCRIPTION_STATUS,
  type Method,
  type Period,
} from "../lib/billing";
import { formatKopecks } from "../lib/credits";
import { formatCalendarDate, formatDate, formatNumber, plural } from "../lib/format";
import { Section } from "../settings/common";
import { Badge } from "../ui/Badge";
import { Button } from "../ui/Button";
import { buttonClass } from "../ui/buttonClass";
import { Notice } from "../ui/Notice";
import { Table } from "../ui/Table";
import { useToast } from "../ui/useToast";
import styles from "./Company.module.css";
import { ChoiceCards, type Choice } from "./CompanySettingsPage";

type Billing = Schemas["BillingResponse"];
type Invoice = Schemas["InvoiceResponse"];
type Act = Schemas["ActResponse"];

function seatsText(n: number): string {
  return `${formatNumber(n)} ${plural(n, "место", "места", "мест")}`;
}

function periodChoices(billing: Billing): Choice<Period>[] {
  return billing.quotes.map((quote) => ({
    value: quote.period,
    title: PERIOD_TITLE[quote.period],
    aside: quote.discount_percent ? (
      <Badge tone="accent">−{formatNumber(quote.discount_percent)} %</Badge>
    ) : null,
    text: (
      <>
        <span className={styles.choicePrice}>{formatKopecks(quote.amount_kopecks)}</span>
        <span>
          {PERIOD_FOR[quote.period]}, {seatsText(billing.seats)}
        </span>
      </>
    ),
  }));
}

const METHODS: Choice<Method>[] = [
  {
    value: "invoice",
    title: METHOD_TITLE.invoice,
    text: "Оплата с расчётного счёта, за месяц, квартал или год. Номер счёта в назначении платежа — и оплата зачтётся сама.",
  },
  {
    value: "card",
    title: METHOD_TITLE.card,
    text: "Помесячно: первый платёж по ссылке, дальше банк списывает раз в месяц. Чек — на почту.",
  },
];

/**
 * Оплата подписки (решения владельца 09.10): период — месяц, квартал или
 * год (квартал и год со скидкой), способ — счёт юрлицу или карта с
 * автосписанием. У оплаченной подписки выбор действует со следующего
 * счёта. Корпоративный тариф — по договору.
 */
export function SubscriptionSection({ billing }: { billing: Billing }) {
  const sub = billing.subscription;
  const queryClient = useQueryClient();
  const toast = useToast();
  const [period, setPeriod] = useState<Period>(sub?.period ?? "month");
  const [method, setMethod] = useState<Method>(
    sub?.payment_method ?? (billing.requisites ? "invoice" : "card"),
  );
  const choose = useMutation({
    mutationFn: (body: Schemas["SubscriptionChoiceRequest"]) =>
      unwrap(api.POST("/api/v1/billing/subscription", { body })),
    onSuccess: (data) => {
      void queryClient.invalidateQueries({ queryKey: BILLING_KEY });
      if (!data.invoice) toast.show("Сохранено: действует со следующего счёта");
    },
  });

  if (billing.seat_price_kopecks === null) {
    return (
      <Section title="Оплата подписки">
        <p>Корпоративный тариф оплачивается по договору — счета пришлёт команда kronto.</p>
      </Section>
    );
  }

  const paid = sub?.current_end != null && sub.status !== "cancelled";
  const cardYear = method === "card" && period !== "month";
  const needsRequisites = method === "invoice" && !billing.requisites;
  const issued = choose.data?.invoice ?? null;
  const status = sub ? SUBSCRIPTION_STATUS[sub.status] : null;

  return (
    <Section
      title="Оплата подписки"
      description={`Квартал и год — со скидкой. Места добавляются сразу с доплатой за оставшиеся дни, сокращаются со следующего периода. Оплату ждём ${formatNumber(billing.grace_days)} ${plural(billing.grace_days, "день", "дня", "дней")} после конца оплаченного периода.`}
    >
      {sub && status ? (
        <div>
          <p>
            <Badge tone={status.tone}>{status.label}</Badge>{" "}
            {sub.current_end
              ? `Оплачено по ${formatCalendarDate(sub.current_end, -1)}`
              : "Первый счёт ещё не оплачен"}
            {" · "}
            {PERIOD_TITLE[sub.period].toLowerCase()},{" "}
            {METHOD_TITLE[sub.payment_method].toLowerCase()}
          </p>
          {sub.payment_method === "card" && sub.card_amount_kopecks ? (
            <p className={`muted ${styles.small}`}>
              Автосписание {formatKopecks(sub.card_amount_kopecks)} раз в месяц. Изменится число
              мест — пришлём новую ссылку: сумму автосписания банк не меняет.
            </p>
          ) : null}
          {sub.next_seats ? (
            <p className={`muted ${styles.small}`}>
              Со следующего периода — {seatsText(sub.next_seats)}.
            </p>
          ) : null}
          {sub.status === "overdue" ? (
            <Notice kind="error" title="Подписка не оплачена">
              Оплатите счёт ниже или напишите нам, если нужна отсрочка.
            </Notice>
          ) : null}
        </div>
      ) : null}
      <ChoiceCards
        label="Период оплаты"
        value={period}
        options={periodChoices(billing)}
        onChange={(value) => {
          setPeriod(value);
          if (value !== "month") setMethod("invoice");
        }}
      />
      <ChoiceCards
        label="Способ оплаты"
        value={method}
        options={METHODS}
        onChange={(value) => {
          setMethod(value);
          if (value === "card") setPeriod("month");
        }}
      />
      {needsRequisites ? (
        <Notice kind="warn">
          Для счёта нужны реквизиты компании —{" "}
          <Link to="/admin/settings">заполните их в настройках</Link>.
        </Notice>
      ) : null}
      {choose.isError ? <Notice kind="error">{errorMessage(choose.error)}</Notice> : null}
      {issued ? <IssuedNotice invoice={issued} /> : null}
      <div className={styles.actions}>
        <Button
          size="sm"
          busy={choose.isPending}
          disabled={needsRequisites || cardYear}
          onClick={() => choose.mutate({ period, payment_method: method })}
        >
          {paid
            ? "Сохранить для следующего периода"
            : method === "card"
              ? "Получить ссылку на оплату"
              : "Выставить счёт"}
        </Button>
      </div>
    </Section>
  );
}

/** Что делать с только что выставленным счётом или ссылкой. */
export function IssuedNotice({ invoice }: { invoice: Invoice }) {
  if (invoice.payment_url) {
    return (
      <Notice kind="ok" title={`Ссылка на оплату ${formatKopecks(invoice.amount_kopecks)}`}>
        <p>Оплата картой или через СБП, чек придёт на почту.</p>
        <a
          className={buttonClass("dark", "sm")}
          href={invoice.payment_url}
          target="_blank"
          rel="noopener noreferrer"
        >
          Оплатить
        </a>
      </Notice>
    );
  }
  return (
    <Notice kind="ok" title={`Счёт ${invoice.number} на ${formatKopecks(invoice.amount_kopecks)}`}>
      Скачайте его в списке счетов ниже. В назначении платежа укажите номер {invoice.number} — тогда
      оплата зачтётся автоматически.
    </Notice>
  );
}

/** Счета и акты компании: скачать PDF, оплатить по ссылке. */
export function DocumentsSection({ billing }: { billing: Billing }) {
  return (
    <Section
      title="Счета и акты"
      description="Акт за месяц появляется в начале следующего месяца и приходит на почту для документов из реквизитов."
    >
      {billing.invoices.length === 0 ? (
        <p className={`muted ${styles.small}`}>Счетов пока нет</p>
      ) : (
        <InvoicesTable invoices={billing.invoices} />
      )}
      {billing.acts.length > 0 ? <ActsTable acts={billing.acts} /> : null}
    </Section>
  );
}

function useDownload() {
  const toast = useToast();
  return useMutation({
    mutationFn: (run: () => Promise<void>) => run(),
    onError: (error) => toast.show(errorMessage(error)),
  });
}

function InvoicesTable({ invoices }: { invoices: Invoice[] }) {
  const download = useDownload();
  return (
    <Table label="Счета">
      <thead>
        <tr>
          <th>Счёт</th>
          <th>За что</th>
          <th>Сумма</th>
          <th>Статус</th>
          <th>
            <span className="visually-hidden">Действия</span>
          </th>
        </tr>
      </thead>
      <tbody>
        {invoices.map((invoice) => {
          const status = INVOICE_STATUS[invoice.status];
          return (
            <tr key={invoice.id}>
              <td className="mono">{invoice.number}</td>
              <td>
                {INVOICE_KIND[invoice.kind]}
                {invoice.period_start && invoice.period_end ? (
                  <span className="muted">
                    {" "}
                    · {formatCalendarDate(invoice.period_start)} —{" "}
                    {formatCalendarDate(invoice.period_end, -1)}
                  </span>
                ) : null}
              </td>
              <td className="num">{formatKopecks(invoice.amount_kopecks)}</td>
              <td>
                <Badge tone={status.tone}>{status.label}</Badge>
                {invoice.status === "awaiting_payment" && invoice.due_date ? (
                  <span className="muted"> до {formatCalendarDate(invoice.due_date)}</span>
                ) : null}
                {invoice.status === "paid" ? (
                  <span className="muted"> {formatDate(invoice.paid_at)}</span>
                ) : null}
              </td>
              <td>
                {invoice.payment_url ? (
                  <a
                    className={buttonClass("dark", "xs")}
                    href={invoice.payment_url}
                    target="_blank"
                    rel="noopener noreferrer"
                  >
                    Оплатить
                  </a>
                ) : null}
                {invoice.has_pdf ? (
                  <Button
                    variant="ghost"
                    size="xs"
                    aria-label={`Скачать счёт ${invoice.number}`}
                    onClick={() => download.mutate(() => downloadInvoice(invoice))}
                  >
                    PDF
                  </Button>
                ) : null}
              </td>
            </tr>
          );
        })}
      </tbody>
    </Table>
  );
}

function ActsTable({ acts }: { acts: Act[] }) {
  const download = useDownload();
  return (
    <Table label="Акты">
      <thead>
        <tr>
          <th>Акт</th>
          <th>Месяц</th>
          <th>Сумма</th>
          <th>
            <span className="visually-hidden">Действия</span>
          </th>
        </tr>
      </thead>
      <tbody>
        {acts.map((act) => (
          <tr key={act.id}>
            <td>№ {act.number}</td>
            <td>{monthName(act.month)}</td>
            <td className="num">{formatKopecks(act.amount_kopecks)}</td>
            <td>
              <Button
                variant="ghost"
                size="xs"
                aria-label={`Скачать акт за ${monthName(act.month)}`}
                onClick={() => download.mutate(() => downloadAct(act))}
              >
                PDF
              </Button>
            </td>
          </tr>
        ))}
      </tbody>
    </Table>
  );
}
