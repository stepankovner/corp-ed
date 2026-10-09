import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Landmark } from "lucide-react";
import { useId, useState, type SubmitEvent } from "react";

import { ConfirmDialog } from "../admin/common";
import styles from "../admin/Admin.module.css";
import { api, unwrap, type Schemas } from "../api/client";
import { errorMessage } from "../api/errors";
import {
  INVOICE_KIND,
  INVOICE_STATUS,
  METHOD_TITLE,
  PERIOD_TITLE,
  SUBSCRIPTION_STATUS,
} from "../lib/billing";
import { formatKopecks } from "../lib/credits";
import { formatCalendarDate, formatDateTime, formatRelative } from "../lib/format";
import { useDocumentTitle } from "../lib/title";
import { Section } from "../settings/common";
import { Badge } from "../ui/Badge";
import { Button } from "../ui/Button";
import { SelectField, TextAreaField } from "../ui/Field";
import { Modal } from "../ui/Modal";
import { Notice } from "../ui/Notice";
import { EmptyState } from "../ui/Page";
import pageStyles from "../ui/Page.module.css";
import { PageSpinner } from "../ui/Spinner";
import { Table } from "../ui/Table";
import tableStyles from "../ui/Table.module.css";
import { useToast } from "../ui/useToast";
import { STAFF_KEY } from "./keys";

type Payment = Schemas["StaffPaymentResponse"];
type Invoice = Schemas["StaffInvoiceResponse"];

const BILLING = [...STAFF_KEY, "billing"] as const;
const MAX_NOTE = 500;

/** Почему платёж не зачёлся сам — коды из payment_service.py. */
const PROBLEMS: Record<string, string> = {
  amount_differs: "сумма не совпала со счётом",
  inn_differs: "ИНН плательщика не тот, что в счёте",
  already_paid: "счёт уже оплачен — возможно, заплатили дважды",
  invoice_cancelled: "счёт отменён",
  wrong_method: "перевод по счёту, выставленному для оплаты картой",
  unknown_invoice: "счёта с таким номером нет",
  no_invoice_number: "в назначении нет номера счёта",
  unknown_operation: "банк прислал операцию, которой у нас нет",
  bank_not_confirmed: "банк не подтвердил оплату счёта",
  bank_unavailable: "банк не ответил на проверку",
  no_awaiting_invoice: "нет счёта, который ждёт этого списания",
  no_bank_invoice: "счёт не выставлен в банке",
};

/**
 * Оплата (решения владельца 09.10): подписки компаний (просроченные
 * первыми — блокирует компанию только команда, кнопкой во вкладке
 * «Компании»), платежи из банка, которые не зачлись сами, и счета, ждущие
 * оплаты. Пока банк не подключён, оплату пакетов отмечают в «Кредитах».
 */
export function PaymentsTab() {
  useDocumentTitle("Оплата");
  const overview = useQuery({
    queryKey: [...BILLING, "overview"],
    queryFn: () => unwrap(api.GET("/api/v1/staff/billing")),
  });
  if (overview.isPending) return <PageSpinner />;
  if (overview.isError) return <Notice kind="error">{errorMessage(overview.error)}</Notice>;
  const data = overview.data;
  return (
    <>
      <div className={styles.tabHead}>
        <p className={styles.tabIntro}>
          Платежи из банка зачитываются сами, если номер счёта, сумма и ИНН совпали и банк
          подтвердил оплату. Остальные ждут здесь: зачтите в счёт или закройте с комментарием.
        </p>
      </div>
      {data.enabled ? null : (
        <Notice kind="info" title="Банк не подключён">
          Оплата через банк выключена (PAYMENTS_PROVIDER=none): счета выставляет команда, оплату
          пакетов отмечают во вкладке «Кредиты».
        </Notice>
      )}
      <PaymentsSection />
      <InvoicesSection />
      <SubscriptionsSection items={data.subscriptions} />
    </>
  );
}

function PaymentsSection() {
  const payments = useQuery({
    queryKey: [...BILLING, "payments"],
    queryFn: () =>
      unwrap(
        api.GET("/api/v1/staff/billing/payments", { params: { query: { status: "review" } } }),
      ),
  });
  const [resolving, setResolving] = useState<Payment | null>(null);
  return (
    <Section title="Платежи на разбор">
      {payments.isPending ? (
        <PageSpinner />
      ) : payments.isError ? (
        <Notice kind="error">{errorMessage(payments.error)}</Notice>
      ) : payments.data.length === 0 ? (
        <EmptyState icon={<Landmark size={32} aria-hidden />} title="Разбирать нечего">
          <p>Все платежи из банка зачлись сами.</p>
        </EmptyState>
      ) : (
        <ul className={styles.cards} aria-label="Платежи на разбор">
          {payments.data.map((payment) => (
            <PaymentCard key={payment.id} payment={payment} onResolve={setResolving} />
          ))}
        </ul>
      )}
      {resolving ? <ResolveDialog payment={resolving} onClose={() => setResolving(null)} /> : null}
    </Section>
  );
}

function PaymentCard({
  payment,
  onResolve,
}: {
  payment: Payment;
  onResolve: (payment: Payment) => void;
}) {
  const titleId = useId();
  const problem = payment.problem ? (PROBLEMS[payment.problem] ?? payment.problem) : null;
  return (
    <li className={styles.card} aria-labelledby={titleId}>
      <div className={styles.cardHead}>
        <div style={{ minWidth: 0 }}>
          <p id={titleId} className={styles.cardTitle}>
            {payment.amount_kopecks !== null ? formatKopecks(payment.amount_kopecks) : "Сумма —"}
            {payment.payer_name ? ` · ${payment.payer_name}` : ""}
          </p>
          <span className={tableStyles.sub} title={formatDateTime(payment.created_at)}>
            {payment.kind === "incoming" ? "перевод по реквизитам" : "оплата по ссылке"} ·{" "}
            {formatRelative(payment.created_at)}
          </span>
        </div>
        <Badge tone={payment.status === "pending" ? "muted" : "warn"}>
          {payment.status === "pending" ? "ждём банк" : "на разбор"}
        </Badge>
      </div>
      <dl className={styles.kv}>
        {problem ? (
          <>
            <dt>Причина</dt>
            <dd>{problem}</dd>
          </>
        ) : null}
        {payment.payer_inn ? (
          <>
            <dt>ИНН плательщика</dt>
            <dd className="mono">{payment.payer_inn}</dd>
          </>
        ) : null}
        {payment.purpose ? (
          <>
            <dt>Назначение</dt>
            <dd style={{ overflowWrap: "anywhere" }}>{payment.purpose}</dd>
          </>
        ) : null}
        {payment.company_name ? (
          <>
            <dt>Компания</dt>
            <dd>{payment.company_name}</dd>
          </>
        ) : null}
      </dl>
      <div className={styles.cardFoot}>
        <Button size="sm" onClick={() => onResolve(payment)}>
          Разобрать
        </Button>
      </div>
    </li>
  );
}

/** Зачесть в счёт или закрыть без зачёта. Окно монтируется на время разбора. */
function ResolveDialog({ payment, onClose }: { payment: Payment; onClose: () => void }) {
  const formId = useId();
  const toast = useToast();
  const queryClient = useQueryClient();
  const invoices = useQuery({
    queryKey: [...BILLING, "invoices", "awaiting_payment"],
    queryFn: () =>
      unwrap(
        api.GET("/api/v1/staff/billing/invoices", {
          params: { query: { status: "awaiting_payment" } },
        }),
      ),
  });
  const [invoiceId, setInvoiceId] = useState(payment.invoice_id ?? "");
  const [note, setNote] = useState("");
  const [problem, setProblem] = useState<string | null>(null);
  const resolve = useMutation({
    mutationFn: (body: Schemas["StaffPaymentResolveRequest"]) =>
      unwrap(
        api.POST("/api/v1/staff/billing/payments/{event_id}/resolve", {
          params: { path: { event_id: payment.id } },
          body,
        }),
      ),
    onSuccess: () => {
      toast.show(invoiceId ? "Платёж зачтён, услуга зачислена" : "Платёж закрыт без зачёта");
      onClose();
    },
    onSettled: () => queryClient.invalidateQueries({ queryKey: STAFF_KEY }),
  });
  const chosen = invoices.data?.find((item) => item.id === invoiceId) ?? null;

  function submit(event: SubmitEvent) {
    event.preventDefault();
    const text = note.trim();
    if (!chosen && !text) {
      setProblem("Без зачёта нужен комментарий: почему платёж закрыт.");
      return;
    }
    resolve.mutate(
      chosen
        ? { tenant_id: chosen.tenant_id, invoice_id: chosen.id, note: text || null }
        : { note: text },
    );
  }

  return (
    <Modal
      open
      onOpenChange={(open) => !open && !resolve.isPending && onClose()}
      title="Разобрать платёж"
      description={
        payment.amount_kopecks !== null
          ? `${formatKopecks(payment.amount_kopecks)}${payment.payer_name ? `, ${payment.payer_name}` : ""}`
          : undefined
      }
      footer={
        <>
          <Button variant="ghost" size="sm" onClick={onClose} disabled={resolve.isPending}>
            Отмена
          </Button>
          <Button type="submit" form={formId} size="sm" busy={resolve.isPending}>
            {chosen ? "Зачесть" : "Закрыть без зачёта"}
          </Button>
        </>
      }
    >
      <form id={formId} className={pageStyles.form} onSubmit={submit} noValidate>
        {resolve.isError ? <Notice kind="error">{errorMessage(resolve.error)}</Notice> : null}
        <SelectField
          label="Счёт"
          value={invoiceId}
          onChange={(e) => {
            setInvoiceId(e.target.value);
            setProblem(null);
          }}
        >
          <option value="">Не зачитывать</option>
          {invoices.data?.map((item) => (
            <option key={item.id} value={item.id}>
              {item.number} · {item.company_name} · {formatKopecks(item.amount_kopecks)}
            </option>
          ))}
        </SelectField>
        {chosen && payment.amount_kopecks !== chosen.amount_kopecks ? (
          <Notice kind="warn">
            Сумма платежа не совпадает со счётом: услуга зачислится полностью, разницу решите с
            клиентом.
          </Notice>
        ) : null}
        <TextAreaField
          label="Комментарий"
          optional={Boolean(chosen)}
          maxLength={MAX_NOTE}
          rows={2}
          value={note}
          onChange={(e) => {
            setNote(e.target.value);
            setProblem(null);
          }}
          placeholder="Например: вернули плательщику 12.10"
          error={problem}
        />
      </form>
    </Modal>
  );
}

function InvoicesSection() {
  const invoices = useQuery({
    queryKey: [...BILLING, "invoices", "awaiting_payment"],
    queryFn: () =>
      unwrap(
        api.GET("/api/v1/staff/billing/invoices", {
          params: { query: { status: "awaiting_payment" } },
        }),
      ),
  });
  return (
    <Section
      title="Счета, ждущие оплаты"
      description="Деньги пришли мимо автоматики (с другого счёта, наличными по договору) — отметьте оплату вручную, услуга зачислится так же, как по вебхуку."
    >
      {invoices.isPending ? (
        <PageSpinner />
      ) : invoices.isError ? (
        <Notice kind="error">{errorMessage(invoices.error)}</Notice>
      ) : invoices.data.length === 0 ? (
        <p className="muted">Неоплаченных счетов нет</p>
      ) : (
        <ul className={styles.cards} aria-label="Счета, ждущие оплаты">
          {invoices.data.map((invoice) => (
            <InvoiceCard key={invoice.id} invoice={invoice} />
          ))}
        </ul>
      )}
    </Section>
  );
}

function InvoiceCard({ invoice }: { invoice: Invoice }) {
  const titleId = useId();
  const toast = useToast();
  const queryClient = useQueryClient();
  const [confirming, setConfirming] = useState<"paid" | "cancel" | null>(null);
  const act = useMutation({
    mutationFn: (action: "paid" | "cancel") => {
      const params = { path: { tenant_id: invoice.tenant_id, invoice_id: invoice.id } };
      return unwrap(
        action === "paid"
          ? api.POST("/api/v1/staff/companies/{tenant_id}/invoices/{invoice_id}/paid", { params })
          : api.POST("/api/v1/staff/companies/{tenant_id}/invoices/{invoice_id}/cancel", {
              params,
            }),
      );
    },
    onSuccess: (_, action) =>
      toast.show(
        action === "paid"
          ? `Счёт ${invoice.number} оплачен: ${invoice.company_name}`
          : `Счёт ${invoice.number} отменён`,
      ),
    onSettled: () => queryClient.invalidateQueries({ queryKey: STAFF_KEY }),
  });
  const status = INVOICE_STATUS[invoice.status];
  return (
    <li className={styles.card} aria-labelledby={titleId}>
      <div className={styles.cardHead}>
        <div style={{ minWidth: 0 }}>
          <p id={titleId} className={styles.cardTitle}>
            {invoice.company_name} · {invoice.number}
          </p>
          <span className={tableStyles.sub}>
            {INVOICE_KIND[invoice.kind]} · {formatKopecks(invoice.amount_kopecks)} ·{" "}
            {METHOD_TITLE[invoice.payment_method].toLowerCase()}
          </span>
        </div>
        <Badge tone={status.tone}>{status.label}</Badge>
      </div>
      <dl className={styles.kv}>
        {invoice.payer_inn ? (
          <>
            <dt>Плательщик</dt>
            <dd>
              {invoice.payer_name}, ИНН <span className="mono">{invoice.payer_inn}</span>
            </dd>
          </>
        ) : null}
        {invoice.due_date ? (
          <>
            <dt>Оплатить до</dt>
            <dd>{formatCalendarDate(invoice.due_date)}</dd>
          </>
        ) : null}
      </dl>
      <div className={styles.cardFoot}>
        <Button size="sm" onClick={() => setConfirming("paid")}>
          Оплачен
        </Button>
        <Button variant="ghost" size="sm" onClick={() => setConfirming("cancel")}>
          Отменить
        </Button>
      </div>
      <ConfirmDialog
        open={confirming === "paid"}
        onOpenChange={(next) => !next && setConfirming(null)}
        title={`Счёт ${invoice.number} оплачен?`}
        description={`${invoice.company_name}: ${INVOICE_KIND[invoice.kind].toLowerCase()} зачислится сразу, администраторам придёт уведомление.`}
        confirmLabel="Оплачен"
        danger={false}
        onConfirm={() => act.mutateAsync("paid")}
      />
      <ConfirmDialog
        open={confirming === "cancel"}
        onOpenChange={(next) => !next && setConfirming(null)}
        title={`Отменить счёт ${invoice.number}?`}
        description="Счёт в банке тоже удалится; пришедшая по нему оплата попадёт на разбор."
        confirmLabel="Отменить счёт"
        onConfirm={() => act.mutateAsync("cancel")}
      />
    </li>
  );
}

function SubscriptionsSection({ items }: { items: Schemas["StaffSubscriptionResponse"][] }) {
  return (
    <Section
      title="Подписки"
      description="Просроченные — первыми. Компанию не блокируем автоматически: приостановить можно во вкладке «Компании»."
    >
      {items.length === 0 ? (
        <p className="muted">Подписок пока нет</p>
      ) : (
        <Table label="Подписки">
          <thead>
            <tr>
              <th>Компания</th>
              <th>Статус</th>
              <th>Оплата</th>
              <th>Оплачено по</th>
              <th>Места</th>
            </tr>
          </thead>
          <tbody>
            {items.map((item) => {
              const status = SUBSCRIPTION_STATUS[item.status];
              return (
                <tr key={item.tenant_id}>
                  <td>
                    {item.company_name}
                    <span className={tableStyles.sub}> {item.company_code}</span>
                  </td>
                  <td>
                    <Badge tone={status.tone}>{status.label}</Badge>
                    {item.is_active ? null : <span className="muted"> приостановлена</span>}
                  </td>
                  <td>
                    {PERIOD_TITLE[item.period]}, {METHOD_TITLE[item.payment_method].toLowerCase()}
                  </td>
                  <td>{item.current_end ? formatCalendarDate(item.current_end, -1) : "—"}</td>
                  <td className="num">
                    {item.seats}
                    {item.next_seats ? ` → ${item.next_seats}` : ""}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </Table>
      )}
    </Section>
  );
}
