import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Coins } from "lucide-react";
import { useId, useState, type SubmitEvent } from "react";

import { ConfirmDialog } from "../admin/common";
import styles from "../admin/Admin.module.css";
import { api, unwrap, type Schemas } from "../api/client";
import { errorMessage } from "../api/errors";
import { credits, formatKopecks } from "../lib/credits";
import { formatDateTime, formatRelative } from "../lib/format";
import { useDocumentTitle } from "../lib/title";
import { Section } from "../settings/common";
import { Badge, type Tone } from "../ui/Badge";
import { Button } from "../ui/Button";
import { SelectField, TextAreaField, TextField } from "../ui/Field";
import { Notice } from "../ui/Notice";
import { EmptyState } from "../ui/Page";
import pageStyles from "../ui/Page.module.css";
import { SegmentedControl, type SegmentOption } from "../ui/SegmentedControl";
import { PageSpinner } from "../ui/Spinner";
import tableStyles from "../ui/Table.module.css";
import { useToast } from "../ui/useToast";
import { STAFF_KEY } from "./keys";

type Order = Schemas["StaffCreditOrderResponse"];
type Filter = "awaiting_payment" | "all";

const FILTERS: SegmentOption<Filter>[] = [
  { value: "awaiting_payment", label: "Ждут оплаты" },
  { value: "all", label: "Все" },
];

const STATUS: Record<Order["status"], { label: string; tone: Tone }> = {
  awaiting_payment: { label: "ждёт оплаты", tone: "warn" },
  paid: { label: "оплачен", tone: "ok" },
  cancelled: { label: "отменён", tone: "muted" },
};

/** Как в схеме бэкенда (StaffCreditGrantRequest). */
const MAX_GRANT = 100_000;
const MAX_COMMENT = 500;

/**
 * Кредиты (решение владельца 09.10): заказы пакетов от администраторов
 * компаний — оплата пока по счёту, команда отмечает «Оплачен», и кредиты
 * зачисляются на 12 месяцев; неоплаченный заказ можно отменить. Ниже —
 * ручное начисление с комментарием (бонус, компенсация). Всё — в журнал
 * компании с учёткой команды.
 */
export function CreditsTab() {
  useDocumentTitle("Кредиты");
  const [filter, setFilter] = useState<Filter>("awaiting_payment");
  const orders = useQuery({
    queryKey: [...STAFF_KEY, "credit-orders", filter],
    queryFn: () =>
      unwrap(
        api.GET("/api/v1/staff/credit-orders", {
          params: { query: { status: filter } },
        }),
      ),
  });

  return (
    <>
      <div className={styles.tabHead}>
        <p className={styles.tabIntro}>
          Заказы пакетов кредитов. Выставьте счёт по номеру заказа и отметьте оплату — кредиты
          зачислятся сразу, администраторам компании придёт уведомление.
        </p>
      </div>
      <div className={styles.toolbar}>
        <SegmentedControl<Filter>
          label="Какие заказы"
          value={filter}
          options={FILTERS}
          onChange={setFilter}
        />
      </div>
      {orders.isPending ? (
        <PageSpinner />
      ) : orders.isError ? (
        <Notice kind="error">{errorMessage(orders.error)}</Notice>
      ) : orders.data.length === 0 ? (
        <EmptyState
          icon={<Coins size={32} aria-hidden />}
          title={filter === "all" ? "Заказов пока нет" : "Заказов, ждущих оплаты, нет"}
        >
          <p>Заказ появляется, когда администратор компании выбирает пакет на странице тарифа.</p>
        </EmptyState>
      ) : (
        <ul className={styles.cards} aria-label="Заказы пакетов">
          {orders.data.map((order) => (
            <OrderCard key={order.id} order={order} />
          ))}
        </ul>
      )}
      <GrantForm />
    </>
  );
}

function OrderCard({ order }: { order: Order }) {
  const titleId = useId();
  const toast = useToast();
  const queryClient = useQueryClient();
  const [confirming, setConfirming] = useState<"paid" | "cancel" | null>(null);
  const act = useMutation({
    mutationFn: (action: "paid" | "cancel") =>
      unwrap(
        action === "paid"
          ? api.POST("/api/v1/staff/companies/{tenant_id}/credit-orders/{order_id}/paid", {
              params: { path: { tenant_id: order.tenant_id, order_id: order.id } },
            })
          : api.POST("/api/v1/staff/companies/{tenant_id}/credit-orders/{order_id}/cancel", {
              params: { path: { tenant_id: order.tenant_id, order_id: order.id } },
            }),
      ),
    onSuccess: (_, action) =>
      toast.show(
        action === "paid"
          ? `Кредиты зачислены: ${order.company_name}, ${credits(order.credits)}`
          : `Заказ № ${order.number} отменён`,
      ),
    onSettled: () => queryClient.invalidateQueries({ queryKey: STAFF_KEY }),
  });
  const status = STATUS[order.status];
  const open = order.status === "awaiting_payment";

  return (
    <li className={styles.card} aria-labelledby={titleId}>
      <div className={styles.cardHead}>
        <div style={{ minWidth: 0 }}>
          <p id={titleId} className={styles.cardTitle} style={{ overflowWrap: "anywhere" }}>
            {order.company_name} · заказ № {order.number}
          </p>
          <span className={tableStyles.sub} title={formatDateTime(order.created_at)}>
            {credits(order.credits)} · {formatKopecks(order.amount_kopecks)} ·{" "}
            {formatRelative(order.created_at)}
          </span>
        </div>
        <Badge tone={status.tone}>{status.label}</Badge>
      </div>
      <dl className={styles.kv}>
        <dt>Номер для счёта</dt>
        <dd className="mono">
          {order.company_code}-{order.number}
        </dd>
        <dt>id компании</dt>
        <dd className="mono">{order.company_ref}</dd>
        <dt>Оплата</dt>
        <dd>{order.payment_method === "invoice" ? "по счёту" : "картой"}</dd>
      </dl>
      {open ? (
        <div className={styles.cardFoot}>
          <Button size="sm" onClick={() => setConfirming("paid")}>
            Оплачен
          </Button>
          <Button variant="ghost" size="sm" onClick={() => setConfirming("cancel")}>
            Отменить
          </Button>
        </div>
      ) : null}
      <ConfirmDialog
        open={confirming === "paid"}
        onOpenChange={(next) => !next && setConfirming(null)}
        title={`Заказ № ${order.number} оплачен?`}
        description={`${order.company_name} получит ${credits(order.credits)} на 12 месяцев. Отменить зачисление из панели нельзя.`}
        confirmLabel="Оплачен, зачислить"
        danger={false}
        onConfirm={() => act.mutateAsync("paid")}
      />
      <ConfirmDialog
        open={confirming === "cancel"}
        onOpenChange={(next) => !next && setConfirming(null)}
        title={`Отменить заказ № ${order.number}?`}
        description="Заказ не будет оплачен, кредиты не зачислятся."
        confirmLabel="Отменить заказ"
        onConfirm={() => act.mutateAsync("cancel")}
      />
    </li>
  );
}

/** Начисление без заказа: бонус, компенсация. Комментарий — в журнал компании. */
function GrantForm() {
  const formId = useId();
  const toast = useToast();
  const queryClient = useQueryClient();
  const companies = useQuery({
    queryKey: [...STAFF_KEY, "companies"],
    queryFn: () => unwrap(api.GET("/api/v1/staff/companies")),
  });
  const [tenantId, setTenantId] = useState("");
  const [amount, setAmount] = useState("");
  const [comment, setComment] = useState("");
  const [problems, setProblems] = useState<Record<string, string>>({});
  const grant = useMutation({
    mutationFn: (body: { tenant_id: string; credits: number; comment: string }) =>
      unwrap(
        api.POST("/api/v1/staff/companies/{tenant_id}/credits", {
          params: { path: { tenant_id: body.tenant_id } },
          body: { credits: body.credits, comment: body.comment },
        }),
      ),
    onSuccess: (data, body) => {
      const name = companies.data?.find((c) => c.id === body.tenant_id)?.name ?? "";
      toast.show(`Начислено ${credits(data.credits)}: ${name}`);
      setAmount("");
      setComment("");
    },
    onSettled: () => queryClient.invalidateQueries({ queryKey: STAFF_KEY }),
  });

  function submit(event: SubmitEvent) {
    event.preventDefault();
    const count = Number(amount.trim());
    const note = comment.trim();
    const next: Record<string, string> = {};
    if (!tenantId) next.company = "Выберите компанию.";
    if (!Number.isInteger(count) || count < 1 || count > MAX_GRANT) {
      next.amount = `Целое число от 1 до ${credits(MAX_GRANT)}.`;
    }
    if (!note) next.comment = "За что начисляем — для журнала компании.";
    setProblems(next);
    if (Object.keys(next).length === 0) {
      grant.mutate({ tenant_id: tenantId, credits: count, comment: note });
    }
  }

  return (
    <Section
      title="Начислить кредиты"
      description="Бонус или компенсация — без заказа и оплаты. Кредиты действуют 12 месяцев и расходуются после месячного пула; комментарий попадёт в журнал компании, администраторам придёт «Кредиты зачислены»."
    >
      <form id={formId} className={pageStyles.form} onSubmit={submit} noValidate>
        {grant.isError ? <Notice kind="error">{errorMessage(grant.error)}</Notice> : null}
        <SelectField
          label="Компания"
          value={tenantId}
          onChange={(e) => setTenantId(e.target.value)}
          error={problems.company ?? null}
        >
          <option value="">Выберите компанию</option>
          {companies.data?.map((company) => (
            <option key={company.id} value={company.id}>
              {company.name} ({company.company_code})
            </option>
          ))}
        </SelectField>
        <TextField
          label="Кредитов"
          type="number"
          inputMode="numeric"
          min={1}
          max={MAX_GRANT}
          value={amount}
          onChange={(e) => setAmount(e.target.value)}
          error={problems.amount ?? null}
        />
        <TextAreaField
          label="Комментарий"
          maxLength={MAX_COMMENT}
          rows={2}
          value={comment}
          onChange={(e) => setComment(e.target.value)}
          placeholder="Например: компенсация за сбой 08.10"
          error={problems.comment ?? null}
        />
        <div>
          <Button type="submit" size="sm" busy={grant.isPending}>
            Начислить
          </Button>
        </div>
      </form>
    </Section>
  );
}
