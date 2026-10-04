import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { PhoneCall } from "lucide-react";
import { useId, useState } from "react";

import styles from "../admin/Admin.module.css";
import { api, unwrap, type Schemas } from "../api/client";
import { errorMessage } from "../api/errors";
import {
  formatCalendarDate,
  formatDateTime,
  formatNumber,
  formatRelative,
  plural,
} from "../lib/format";
import { tariffByCode } from "../lib/tariffs";
import { useDocumentTitle } from "../lib/title";
import { Badge, type Tone } from "../ui/Badge";
import { Select } from "../ui/Field";
import { Notice } from "../ui/Notice";
import { EmptyState } from "../ui/Page";
import { SegmentedControl, type SegmentOption } from "../ui/SegmentedControl";
import { PageSpinner } from "../ui/Spinner";
import tableStyles from "../ui/Table.module.css";
import { useToast } from "../ui/useToast";
import { STAFF_KEY } from "./keys";

type Lead = Schemas["StaffLeadResponse"];
type Status = Schemas["LeadStatus"];
type Filter = "all" | Status;

/** label — плашка и всплывающее сообщение, option — пункт списка выбора. */
const STATUS: Record<Status, { label: string; option: string; tone: Tone }> = {
  new: { label: "новая", option: "Новая", tone: "warn" },
  contacted: { label: "перезвонили", option: "Перезвонили", tone: "accent" },
  scheduled: { label: "созвон назначен", option: "Созвон назначен", tone: "ok" },
  rejected: { label: "отклонена", option: "Отклонена", tone: "muted" },
};
const STATUSES = Object.keys(STATUS) as Status[];

const FILTERS: SegmentOption<Filter>[] = [
  { value: "new", label: "Новые" },
  { value: "all", label: "Все" },
  { value: "contacted", label: "Перезвонили" },
  { value: "scheduled", label: "Назначен" },
  { value: "rejected", label: "Отклонены" },
];

const EMPTY: Record<Filter, string> = {
  new: "Новых заявок нет",
  all: "Заявок пока нет",
  contacted: "Тех, кому перезвонили, нет",
  scheduled: "Назначенных созвонов нет",
  rejected: "Отклонённых заявок нет",
};

/** Для ссылки tel: — только цифры и плюс, как набирает телефон. */
function telHref(phone: string): string {
  return `tel:${phone.replace(/[^\d+]/g, "")}`;
}

/**
 * Созвоны (ТЗ §9): заявки на созвон со страницы тарифов. Команда
 * перезванивает и отмечает, что с заявкой: перезвонили, созвон назначен
 * или отклонена.
 */
export function LeadsTab() {
  useDocumentTitle("Созвоны");
  const [filter, setFilter] = useState<Filter>("new");
  const leads = useQuery({
    queryKey: [...STAFF_KEY, "leads", filter],
    queryFn: () =>
      unwrap(
        api.GET("/api/v1/staff/leads", {
          params: { query: filter === "all" ? {} : { status: filter } },
        }),
      ),
  });

  return (
    <>
      <div className={styles.tabHead}>
        <p className={styles.tabIntro}>
          Заявки на созвон со страницы тарифов. Перезвоните и отметьте, чем закончилось.
        </p>
      </div>
      <div className={styles.toolbar}>
        <SegmentedControl<Filter>
          label="Статус заявок"
          value={filter}
          options={FILTERS}
          onChange={setFilter}
        />
      </div>
      {leads.isPending ? (
        <PageSpinner />
      ) : leads.isError ? (
        <Notice kind="error">{errorMessage(leads.error)}</Notice>
      ) : leads.data.length === 0 ? (
        <EmptyState icon={<PhoneCall size={32} aria-hidden />} title={EMPTY[filter]}>
          <p>Заявки приходят с формы «Записаться на созвон» на странице тарифов.</p>
        </EmptyState>
      ) : (
        <ul className={styles.cards} aria-label="Заявки на созвон">
          {leads.data.map((lead) => (
            <LeadCard key={lead.id} lead={lead} />
          ))}
        </ul>
      )}
    </>
  );
}

function LeadCard({ lead }: { lead: Lead }) {
  const titleId = useId();
  const toast = useToast();
  const queryClient = useQueryClient();
  const change = useMutation({
    mutationFn: (status: Status) =>
      unwrap(
        api.PATCH("/api/v1/staff/leads/{lead_id}", {
          params: { path: { lead_id: lead.id } },
          body: { status },
        }),
      ),
    onSuccess: (_, status) => toast.show(`${lead.company_name} — ${STATUS[status].label}`),
    onError: (error) => toast.show(errorMessage(error), { tone: "error" }),
    onSettled: () => queryClient.invalidateQueries({ queryKey: STAFF_KEY }),
  });
  // Пока сервер не ответил, список показывает выбранное, а не прежнее.
  const status = change.isPending ? change.variables : lead.status;
  const tariff = tariffByCode(lead.tariff)?.name ?? lead.tariff;

  return (
    <li className={styles.card} aria-labelledby={titleId}>
      <div className={styles.cardHead}>
        <div style={{ minWidth: 0 }}>
          <p id={titleId} className={styles.cardTitle} style={{ overflowWrap: "anywhere" }}>
            {lead.company_name}
          </p>
          <span className={tableStyles.sub} title={formatDateTime(lead.created_at)}>
            заявка {formatRelative(lead.created_at)}
          </span>
        </div>
        <Badge tone={STATUS[lead.status].tone}>{STATUS[lead.status].label}</Badge>
      </div>
      <dl className={styles.kv}>
        <dt>Контакт</dt>
        <dd>{lead.contact_name}</dd>
        <dt>Телефон</dt>
        <dd>
          <a href={telHref(lead.phone)}>{lead.phone}</a>
        </dd>
        {lead.email ? (
          <>
            <dt>Почта</dt>
            <dd>
              <a href={`mailto:${lead.email}`}>{lead.email}</a>
            </dd>
          </>
        ) : null}
        <dt>Тариф</dt>
        <dd>
          «{tariff}», {formatNumber(lead.seats)} {plural(lead.seats, "место", "места", "мест")}
        </dd>
        <dt>Удобное время</dt>
        <dd>
          {formatCalendarDate(lead.preferred_date)}, {lead.preferred_slot} по Москве
        </dd>
        {lead.comment ? (
          <>
            <dt>Комментарий</dt>
            <dd style={{ whiteSpace: "pre-line" }}>{lead.comment}</dd>
          </>
        ) : null}
      </dl>
      <div className={styles.cardFoot}>
        <Select
          compact
          aria-label={`Статус заявки: ${lead.company_name}`}
          value={status}
          disabled={change.isPending}
          onChange={(e) => change.mutate(e.target.value as Status)}
        >
          {STATUSES.map((value) => (
            <option key={value} value={value}>
              {STATUS[value].option}
            </option>
          ))}
        </Select>
      </div>
    </li>
  );
}
