import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { LifeBuoy } from "lucide-react";
import { useId, useState } from "react";

import styles from "../admin/Admin.module.css";
import { api, unwrap, type Schemas } from "../api/client";
import { errorMessage } from "../api/errors";
import { formatDateTime, formatRelative } from "../lib/format";
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

type Request = Schemas["StaffSupportResponse"];
type Status = Request["status"];
type Filter = "all" | Status;

const TOPIC: Record<Request["topic"], string> = {
  login: "вход и учётная запись",
  documents: "документы и подключения",
  answers: "ответы ассистента",
  billing: "тариф и оплата",
  other: "другое",
};

/** label — плашка и всплывающее сообщение, option — пункт списка выбора. */
const STATUS: Record<Status, { label: string; option: string; tone: Tone }> = {
  new: { label: "новое", option: "Новое", tone: "warn" },
  answered: { label: "отвечено", option: "Отвечено", tone: "ok" },
  closed: { label: "закрыто", option: "Закрыто", tone: "muted" },
};
const STATUSES = Object.keys(STATUS) as Status[];

const FILTERS: SegmentOption<Filter>[] = [
  { value: "new", label: "Новые" },
  { value: "all", label: "Все" },
  { value: "answered", label: "Отвечены" },
  { value: "closed", label: "Закрыты" },
];

const EMPTY: Record<Filter, string> = {
  new: "Новых обращений нет",
  all: "Обращений пока нет",
  answered: "Отвеченных обращений нет",
  closed: "Закрытых обращений нет",
};

const SUBJECT = "kronto: ваше обращение";

/** Кто написал: имя, без него — почта. */
function author(request: Request): string {
  return request.name || request.email || "Без имени";
}

/**
 * Обращения (ТЗ §9): вопросы из раздела «Помощь». Команда отвечает
 * письмом и отмечает, что с обращением: отвечено или закрыто. Текст и
 * почта — только здесь, в Telegram их нет.
 */
export function SupportTab() {
  useDocumentTitle("Обращения");
  const [filter, setFilter] = useState<Filter>("new");
  const requests = useQuery({
    queryKey: [...STAFF_KEY, "support", filter],
    queryFn: () =>
      unwrap(
        api.GET("/api/v1/staff/support", {
          params: { query: filter === "all" ? {} : { status: filter } },
        }),
      ),
  });

  return (
    <>
      <div className={styles.tabHead}>
        <p className={styles.tabIntro}>
          Обращения из раздела «Помощь». Ответьте письмом и отметьте, что с обращением.
        </p>
      </div>
      <div className={styles.toolbar}>
        <SegmentedControl<Filter>
          label="Статус обращений"
          value={filter}
          options={FILTERS}
          onChange={setFilter}
        />
      </div>
      {requests.isPending ? (
        <PageSpinner />
      ) : requests.isError ? (
        <Notice kind="error">{errorMessage(requests.error)}</Notice>
      ) : requests.data.length === 0 ? (
        <EmptyState icon={<LifeBuoy size={32} aria-hidden />} title={EMPTY[filter]}>
          <p>Обращения приходят с формы в разделе «Помощь».</p>
        </EmptyState>
      ) : (
        <ul className={styles.cards} aria-label="Обращения">
          {requests.data.map((request) => (
            <RequestCard key={request.id} request={request} />
          ))}
        </ul>
      )}
    </>
  );
}

function RequestCard({ request }: { request: Request }) {
  const titleId = useId();
  const toast = useToast();
  const queryClient = useQueryClient();
  const who = author(request);
  const change = useMutation({
    mutationFn: (status: Status) =>
      unwrap(
        api.PATCH("/api/v1/staff/support/{request_id}", {
          params: { path: { request_id: request.id } },
          body: { status },
        }),
      ),
    onSuccess: (_, status) => toast.show(`${who} — ${STATUS[status].label}`),
    onError: (error) => toast.show(errorMessage(error), { tone: "error" }),
    onSettled: () => queryClient.invalidateQueries({ queryKey: STAFF_KEY }),
  });
  // Пока сервер не ответил, список показывает выбранное, а не прежнее.
  const status = change.isPending ? change.variables : request.status;

  return (
    <li className={styles.card} aria-labelledby={titleId}>
      <div className={styles.cardHead}>
        <div style={{ minWidth: 0 }}>
          <p id={titleId} className={styles.cardTitle} style={{ overflowWrap: "anywhere" }}>
            {who}
          </p>
          <span className={tableStyles.sub} title={formatDateTime(request.created_at)}>
            {TOPIC[request.topic]} · {formatRelative(request.created_at)}
          </span>
        </div>
        <Badge tone={STATUS[request.status].tone}>{STATUS[request.status].label}</Badge>
      </div>
      <dl className={styles.kv}>
        <dt>Почта</dt>
        <dd>
          {request.email ? (
            <a href={`mailto:${request.email}?subject=${encodeURIComponent(SUBJECT)}`}>
              {request.email}
            </a>
          ) : (
            "не указана"
          )}
        </dd>
        <dt>Компания</dt>
        <dd>{request.company || "без компании"}</dd>
      </dl>
      <p
        style={{
          padding: "var(--s-3) var(--s-4)",
          borderRadius: "var(--r-md)",
          background: "var(--surface-2)",
          fontSize: "var(--fs-small)",
          whiteSpace: "pre-wrap",
          overflowWrap: "anywhere",
        }}
      >
        {request.message}
      </p>
      <div className={styles.cardFoot}>
        <Select
          compact
          aria-label={`Статус обращения: ${who}`}
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
