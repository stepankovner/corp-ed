import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { ArrowRight, MessagesSquare } from "lucide-react";
import { useState } from "react";
import { Link } from "react-router";

import { api, unwrap, type Schemas } from "../api/client";
import { errorMessage } from "../api/errors";
import { reasonLabel } from "../chat/feedbackReasons";
import { formatDateTime, formatNumber, plural } from "../lib/format";
import { useDocumentTitle } from "../lib/title";
import { Badge } from "../ui/Badge";
import { Notice } from "../ui/Notice";
import { EmptyState, Page, PageHeader } from "../ui/Page";
import pageStyles from "../ui/Page.module.css";
import { SegmentedControl } from "../ui/SegmentedControl";
import { PageSpinner } from "../ui/Spinner";
import styles from "./Overview.module.css";

type Analytics = Schemas["AnalyticsResponse"];
type Period = "7" | "30" | "90";

const PERIODS: { value: Period; label: string }[] = [
  { value: "7", label: "7 дней" },
  { value: "30", label: "30 дней" },
  { value: "90", label: "90 дней" },
];

/**
 * Главная админки (ТЗ §7): как сотрудники пользуются ассистентом. Только
 * обезличенные числа: частый вопрос виден, когда его задали хотя бы три
 * разных человека; комментарии к 👎 — без автора.
 */
export function OverviewPage() {
  useDocumentTitle("Обзор");
  const [period, setPeriod] = useState<Period>("30");
  const analytics = useQuery({
    queryKey: ["analytics", period],
    queryFn: () =>
      unwrap(api.GET("/api/v1/analytics", { params: { query: { days: Number(period) } } })),
    placeholderData: keepPreviousData,
  });

  return (
    <Page>
      <PageHeader
        label="управление"
        title="Обзор"
        description="Как сотрудники пользуются ассистентом. Без имён: только общие числа и вопросы, которые задали несколько человек."
        actions={
          <SegmentedControl label="Период" value={period} options={PERIODS} onChange={setPeriod} />
        }
      />
      {analytics.isPending ? (
        <PageSpinner />
      ) : analytics.isError ? (
        <Notice kind="error">{errorMessage(analytics.error)}</Notice>
      ) : (
        <div aria-busy={analytics.isFetching} className={styles.body}>
          <OverviewView data={analytics.data} />
        </div>
      )}
    </Page>
  );
}

function share(part: number, total: number): string {
  return total > 0 ? `${Math.round((part / total) * 100)} %` : "—";
}

function OverviewView({ data }: { data: Analytics }) {
  const missed = data.questions - data.answered;
  const rated = data.likes + data.dislikes;
  return (
    <div className={pageStyles.stack}>
      <div className={styles.stats}>
        <Stat value={formatNumber(data.questions)} label="вопросов" />
        <Stat
          value={share(data.answered, data.questions)}
          label="с ответом по документам"
          hint={
            data.questions
              ? `${formatNumber(data.answered)} из ${formatNumber(data.questions)}`
              : undefined
          }
        />
        <Stat
          value={share(missed, data.questions)}
          label="без ответа в документах"
          hint={
            missed
              ? `общий ответ — ${formatNumber(data.general)}, отказ — ${formatNumber(data.refused)}`
              : undefined
          }
        />
        <Stat
          value={formatNumber(data.active_people)}
          label="сотрудников задавали вопросы"
          hint={`из ${formatNumber(data.members)} в компании`}
        />
        <Stat
          value={rated ? share(data.likes, rated) : "—"}
          label="оценок 👍"
          hint={
            rated
              ? `👍 ${formatNumber(data.likes)} · 👎 ${formatNumber(data.dislikes)}`
              : "оценок пока нет"
          }
        />
      </div>

      {data.open_gaps > 0 ? (
        <Link to="/admin/gaps" className={styles.gaps}>
          <span>
            <strong className="num">{formatNumber(data.open_gaps)}</strong>{" "}
            {plural(data.open_gaps, "пробел", "пробела", "пробелов")} в документах ждут разбора —
            вопросы, на которые ассистент не нашёл ответа.
          </span>
          <ArrowRight size={16} aria-hidden />
        </Link>
      ) : null}

      {data.questions === 0 ? (
        <EmptyState icon={<MessagesSquare size={32} aria-hidden />} title="Вопросов пока не было">
          <p>
            Пригласите сотрудников и загрузите документы в разделе{" "}
            <Link to="/admin/sources/files">«Источники»</Link> — здесь появится статистика.
          </p>
        </EmptyState>
      ) : (
        <>
          <DaysChart days={data.days} />
          <Frequent items={data.frequent} />
          <Feedback data={data} />
        </>
      )}

      <p className={`muted ${styles.foot}`}>
        За период израсходовано {formatNumber(data.credits)}{" "}
        {plural(data.credits, "кредит", "кредита", "кредитов")}. Лимит и рабочие места — в разделе{" "}
        <Link to="/admin/tariff">«Тариф»</Link>.
      </p>
    </div>
  );
}

function Stat({ value, label, hint }: { value: string; label: string; hint?: string }) {
  return (
    <div className={styles.stat}>
      <span className={styles.statValue}>{value}</span>
      <span className={styles.statLabel}>{label}</span>
      {hint ? <span className={styles.statHint}>{hint}</span> : null}
    </div>
  );
}

const dayLabel = new Intl.DateTimeFormat("ru-RU", {
  day: "numeric",
  month: "short",
  timeZone: "UTC",
});

/** «2026-10-03» → «3 окт.»: день компании как есть, без пояса браузера. */
function formatDay(value: string): string {
  const [year, month, day] = value.split("-").map(Number) as [number, number, number];
  return dayLabel.format(new Date(Date.UTC(year, month - 1, day)));
}

function DaysChart({ days }: { days: Analytics["days"] }) {
  const max = Math.max(1, ...days.map((day) => day.questions));
  const first = days[0];
  const last = days[days.length - 1];
  return (
    <section className={pageStyles.card} aria-labelledby="overview-days">
      <div className={styles.chartHead}>
        <h2 id="overview-days" className={pageStyles.sectionTitle}>
          Вопросы по дням
        </h2>
        <span className={styles.legend} aria-hidden>
          <span className={styles.legendItem}>
            <span className={`${styles.swatch} ${styles.answered}`} /> с ответом по документам
          </span>
          <span className={styles.legendItem}>
            <span className={`${styles.swatch} ${styles.missed}`} /> без ответа
          </span>
        </span>
      </div>
      <div className={styles.chart} aria-hidden>
        <span className={`num ${styles.max}`}>{formatNumber(max)}</span>
        <div className={styles.bars} style={{ gap: days.length > 40 ? 1 : 3 }}>
          {days.map((day) => (
            <div
              key={day.day}
              className={styles.bar}
              title={`${formatDay(day.day)}: ${formatNumber(day.questions)}, с ответом — ${formatNumber(day.answered)}`}
            >
              <span
                className={styles.missed}
                style={{ height: `${((day.questions - day.answered) / max) * 100}%` }}
              />
              <span
                className={styles.answered}
                style={{ height: `${(day.answered / max) * 100}%` }}
              />
            </div>
          ))}
        </div>
      </div>
      {first && last ? (
        <div className={`mono muted ${styles.axis}`} aria-hidden>
          <span>{formatDay(first.day)}</span>
          <span>{formatDay(last.day)}</span>
        </div>
      ) : null}
      <table className="visually-hidden">
        <caption>Вопросы по дням</caption>
        <thead>
          <tr>
            <th scope="col">День</th>
            <th scope="col">Вопросов</th>
            <th scope="col">С ответом по документам</th>
          </tr>
        </thead>
        <tbody>
          {days.map((day) => (
            <tr key={day.day}>
              <th scope="row">{formatDay(day.day)}</th>
              <td>{day.questions}</td>
              <td>{day.answered}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </section>
  );
}

function Frequent({ items }: { items: Analytics["frequent"] }) {
  return (
    <section className={pageStyles.card} aria-labelledby="overview-frequent">
      <h2 id="overview-frequent" className={pageStyles.sectionTitle}>
        Частые вопросы
      </h2>
      {items.length === 0 ? (
        <p className="muted">
          Пока нет вопросов, которые задали хотя бы три разных сотрудника. Вопросы реже не
          показываем, чтобы по ним нельзя было узнать автора.
        </p>
      ) : (
        <ol className={styles.frequent}>
          {items.map((item) => (
            <li key={item.question} className={styles.frequentItem}>
              <span className={styles.question}>{item.question}</span>
              <span className={`mono muted ${styles.frequentMeta}`}>
                {formatNumber(item.asked)} {plural(item.asked, "раз", "раза", "раз")} ·{" "}
                {formatNumber(item.people)} {plural(item.people, "человек", "человека", "человек")}
              </span>
              {item.answered < item.asked ? (
                <Badge tone={item.answered === 0 ? "warn" : "muted"}>
                  {item.answered === 0
                    ? "ответа нет в документах"
                    : `с ответом ${share(item.answered, item.asked)}`}
                </Badge>
              ) : null}
            </li>
          ))}
        </ol>
      )}
    </section>
  );
}

function Feedback({ data }: { data: Analytics }) {
  const reasons = Object.entries(data.reasons).sort((a, b) => b[1] - a[1]);
  if (!data.dislikes && !data.comments.length) return null;
  return (
    <section className={pageStyles.card} aria-labelledby="overview-feedback">
      <h2 id="overview-feedback" className={pageStyles.sectionTitle}>
        Что не понравилось
      </h2>
      {reasons.length ? (
        <ul className={styles.reasons}>
          {reasons.map(([reason, count]) => (
            <li key={reason}>
              <Badge>
                {reasonLabel(reason)} · <span className="num">{formatNumber(count)}</span>
              </Badge>
            </li>
          ))}
        </ul>
      ) : null}
      {data.comments.length ? (
        <ul className={styles.comments}>
          {data.comments.map((item) => (
            <li key={`${item.created_at}-${item.question}`} className={styles.comment}>
              <p className={styles.commentText}>«{item.comment}»</p>
              <p className="muted">
                На вопрос «{item.question}»{item.reason ? ` · ${reasonLabel(item.reason)}` : ""}
              </p>
              <p className={`mono muted ${styles.commentDate}`}>
                {formatDateTime(item.created_at)}
              </p>
            </li>
          ))}
        </ul>
      ) : (
        <p className="muted">Комментариев к 👎 за этот период нет.</p>
      )}
    </section>
  );
}
