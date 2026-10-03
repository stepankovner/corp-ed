import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { SearchX } from "lucide-react";
import { useState } from "react";

import { api, unwrap, type Schemas } from "../api/client";
import { errorMessage } from "../api/errors";
import { formatDate, formatRelative, plural } from "../lib/format";
import { useDocumentTitle } from "../lib/title";
import { Badge, type Tone } from "../ui/Badge";
import { Select } from "../ui/Field";
import { Notice } from "../ui/Notice";
import { EmptyState, Page, PageHeader } from "../ui/Page";
import { SegmentedControl } from "../ui/SegmentedControl";
import { PageSpinner } from "../ui/Spinner";
import styles from "./Admin.module.css";

type Gap = Schemas["GapClusterResponse"];
type Status = Schemas["GapStatus"];
type Filter = "open" | Status;

const STATUS: Record<Status, { label: string; tone: Tone }> = {
  new: { label: "новый", tone: "warn" },
  in_progress: { label: "в работе", tone: "accent" },
  resolved: { label: "закрыт", tone: "ok" },
  dismissed: { label: "не нужен", tone: "muted" },
};

export function GapsPage() {
  useDocumentTitle("Пробелы в документах");
  const [filter, setFilter] = useState<Filter>("open");
  const gaps = useQuery({
    queryKey: ["gaps", filter],
    queryFn: () =>
      unwrap(
        api.GET("/api/v1/gaps", {
          params: { query: { limit: 100, ...(filter === "open" ? {} : { status: filter }) } },
        }),
      ),
  });
  const clusters = (gaps.data?.clusters ?? []).filter(
    (gap) => filter !== "open" || gap.status === "new" || gap.status === "in_progress",
  );

  return (
    <Page>
      <PageHeader
        label="управление"
        title="Пробелы в документах"
        description="Вопросы сотрудников, на которые в документах не нашлось ответа, сгруппированные по темам. Сверху — самые частые: с них стоит начать дописывать регламенты."
      />
      <div className={styles.toolbar}>
        <SegmentedControl<Filter>
          label="Статус"
          value={filter}
          onChange={setFilter}
          options={[
            { value: "open", label: "Открытые" },
            { value: "new", label: "Новые" },
            { value: "in_progress", label: "В работе" },
            { value: "resolved", label: "Закрытые" },
            { value: "dismissed", label: "Не нужны" },
          ]}
        />
      </div>
      {gaps.isPending ? (
        <PageSpinner />
      ) : gaps.isError ? (
        <Notice kind="error">{errorMessage(gaps.error)}</Notice>
      ) : clusters.length === 0 ? (
        <EmptyState icon={<SearchX size={32} aria-hidden />} title="Пробелов нет">
          <p>Когда сотрудники спросят то, чего нет в документах, темы появятся здесь.</p>
        </EmptyState>
      ) : (
        <ul className={styles.cards}>
          {clusters.map((gap) => (
            <GapCard key={gap.id} gap={gap} />
          ))}
        </ul>
      )}
    </Page>
  );
}

function GapCard({ gap }: { gap: Gap }) {
  const queryClient = useQueryClient();
  const change = useMutation({
    mutationFn: (status: Status) =>
      unwrap(
        api.PATCH("/api/v1/gaps/{cluster_id}", {
          params: { path: { cluster_id: gap.id } },
          body: { status },
        }),
      ),
    onSettled: () => queryClient.invalidateQueries({ queryKey: ["gaps"] }),
  });
  const status = STATUS[gap.status];

  return (
    <li className={styles.card}>
      <div className={styles.cardHead}>
        <div>
          <p className={styles.cardTitle}>{gap.title}</p>
          {gap.missing ? (
            <p className="muted" style={{ fontSize: "var(--fs-small)", marginTop: 4 }}>
              Не хватает: {gap.missing}
            </p>
          ) : null}
        </div>
        <Badge tone={status.tone}>{status.label}</Badge>
      </div>
      <div className={styles.meta}>
        <span>
          {gap.question_count} {plural(gap.question_count, "вопрос", "вопроса", "вопросов")}
        </span>
        <span>
          {gap.user_count} {plural(gap.user_count, "сотрудник", "сотрудника", "сотрудников")}
        </span>
        <span>впервые {formatDate(gap.first_seen)}</span>
        <span>последний {formatRelative(gap.last_seen)}</span>
      </div>
      {gap.sample_questions.length ? (
        <ul className={styles.samples} aria-label="Примеры вопросов">
          {gap.sample_questions.slice(0, 5).map((question) => (
            <li key={question}>«{question}»</li>
          ))}
        </ul>
      ) : null}
      <div className={styles.cardFoot}>
        <Select
          compact
          aria-label="Статус темы"
          value={gap.status}
          disabled={change.isPending}
          onChange={(e) => change.mutate(e.target.value as Status)}
        >
          <option value="new">Новый</option>
          <option value="in_progress">В работе — дописываем документ</option>
          <option value="resolved">Закрыт — документ дополнен</option>
          <option value="dismissed">Не нужен</option>
        </Select>
        {change.isError ? (
          <span className={styles.errorText}>{errorMessage(change.error)}</span>
        ) : null}
      </div>
    </li>
  );
}
