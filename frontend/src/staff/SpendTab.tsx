import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { MessagesSquare } from "lucide-react";
import { useState, type ReactNode } from "react";

import adminStyles from "../admin/Admin.module.css";
import { api, unwrap, type Schemas } from "../api/client";
import { errorMessage } from "../api/errors";
import { formatCalendarDate, formatNumber } from "../lib/format";
import { useDocumentTitle } from "../lib/title";
import { Notice } from "../ui/Notice";
import { EmptyState } from "../ui/Page";
import pageStyles from "../ui/Page.module.css";
import { SegmentedControl } from "../ui/SegmentedControl";
import { PageSpinner } from "../ui/Spinner";
import { Table } from "../ui/Table";
import tableStyles from "../ui/Table.module.css";
import { STAFF_KEY } from "./keys";
import styles from "./Spend.module.css";

type Spend = Schemas["SpendResponse"];
type Period = "7" | "30" | "90";

const PERIODS: { value: Period; label: string }[] = [
  { value: "7", label: "7 дней" },
  { value: "30", label: "30 дней" },
  { value: "90", label: "90 дней" },
];

/**
 * Расход на модель ответа (ТЗ §9): токены из журнала ответов по всем
 * компаниям — по дням, моделям и компаниям. Эмбеддинги в журнал не
 * пишутся, здесь их нет. Рубли — по цене из BILLING_LLM_RUB_PER_1K_TOKENS.
 */
export function SpendTab() {
  useDocumentTitle("Расход");
  const [period, setPeriod] = useState<Period>("30");
  const spend = useQuery({
    queryKey: [...STAFF_KEY, "spend", period],
    queryFn: () =>
      unwrap(api.GET("/api/v1/staff/spend", { params: { query: { days: Number(period) } } })),
    placeholderData: keepPreviousData,
  });

  return (
    <>
      <div className={adminStyles.tabHead}>
        <p className={adminStyles.tabIntro}>
          Сколько стоят ответы: токены модели ответа по всем компаниям. Эмбеддинги сюда не входят.
        </p>
        <div className={adminStyles.tabActions}>
          <SegmentedControl label="Период" value={period} options={PERIODS} onChange={setPeriod} />
        </div>
      </div>
      {spend.isPending ? (
        <PageSpinner />
      ) : spend.isError ? (
        <Notice kind="error">{errorMessage(spend.error)}</Notice>
      ) : (
        <div aria-busy={spend.isFetching} className={styles.body}>
          <SpendView data={spend.data} />
        </div>
      )}
    </>
  );
}

function share(part: number, total: number): string {
  return total > 0 ? `${Math.round((part / total) * 100)} %` : "—";
}

/** Рубли: мелкие суммы — с копейками, крупные — целыми. */
function formatRub(value: number): string {
  return new Intl.NumberFormat("ru-RU", { maximumFractionDigits: value < 100 ? 2 : 0 }).format(
    value,
  );
}

function SpendView({ data }: { data: Spend }) {
  const tokens = data.input_tokens + data.output_tokens;
  const price = data.rub_per_1k_tokens;
  return (
    <div className={pageStyles.stack}>
      <div className={styles.stats}>
        <Stat value={formatNumber(data.questions)} label="вопросов" />
        <Stat
          value={formatNumber(tokens)}
          label="токенов"
          hint={`вход ${formatNumber(data.input_tokens)} / выход ${formatNumber(data.output_tokens)}`}
        />
        <Stat value={formatNumber(data.credits)} label="кредитов" />
        {data.rub !== null ? (
          <Stat
            value={`≈ ${formatRub(data.rub)}\u00a0₽`}
            label="на модель ответа"
            hint={price !== null ? `${formatRub(price)} ₽ за 1 000 токенов` : undefined}
          />
        ) : (
          <Stat
            value="—"
            label="на модель ответа"
            hint={
              <>
                цена не задана: <code>BILLING_LLM_RUB_PER_1K_TOKENS</code>
              </>
            }
          />
        )}
      </div>

      {data.questions === 0 ? (
        <EmptyState icon={<MessagesSquare size={32} aria-hidden />} title="Вопросов не было">
          <p>За этот период ни одна компания не задала вопроса — расхода на модель нет.</p>
        </EmptyState>
      ) : (
        <>
          <DaysChart days={data.days} />
          <Models models={data.models} />
          <Companies companies={data.companies} tokens={tokens} price={price} />
        </>
      )}

      <p className={`muted ${styles.foot}`}>
        Период: с {formatCalendarDate(data.since)} по {formatCalendarDate(data.until)} включительно.
        Токены и кредиты — из журнала ответов.
      </p>
    </div>
  );
}

function Stat({ value, label, hint }: { value: string; label: string; hint?: ReactNode }) {
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

/** «2026-10-03» → «3 окт.»: день биллинга как есть, без пояса браузера. */
function formatDay(value: string): string {
  const [year, month, day] = value.split("-").map(Number) as [number, number, number];
  return dayLabel.format(new Date(Date.UTC(year, month - 1, day)));
}

function DaysChart({ days }: { days: Spend["days"] }) {
  const max = Math.max(1, ...days.map((day) => day.tokens));
  const first = days[0];
  const last = days[days.length - 1];
  return (
    <section className={pageStyles.card} aria-labelledby="spend-days">
      <h2 id="spend-days" className={pageStyles.sectionTitle}>
        Токены по дням
      </h2>
      <div className={styles.chart} aria-hidden>
        <span className={`num ${styles.max}`}>{formatNumber(max)}</span>
        <div className={styles.bars} style={{ gap: days.length > 40 ? 1 : 3 }}>
          {days.map((day) => (
            <div
              key={day.day}
              className={styles.bar}
              title={`${formatDay(day.day)}: ${formatNumber(day.tokens)} токенов, вопросов — ${formatNumber(day.questions)}`}
            >
              <span style={{ height: `${(day.tokens / max) * 100}%` }} />
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
        <caption>Токены по дням</caption>
        <thead>
          <tr>
            <th scope="col">День</th>
            <th scope="col">Токенов</th>
            <th scope="col">Вопросов</th>
            <th scope="col">Кредитов</th>
          </tr>
        </thead>
        <tbody>
          {days.map((day) => (
            <tr key={day.day}>
              <th scope="row">{formatDay(day.day)}</th>
              <td>{day.tokens}</td>
              <td>{day.questions}</td>
              <td>{day.credits}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </section>
  );
}

function Models({ models }: { models: Spend["models"] }) {
  return (
    <section className={styles.tables} aria-labelledby="spend-models">
      <h2 id="spend-models" className={pageStyles.sectionTitle}>
        По моделям
      </h2>
      <Table label="Расход по моделям">
        <thead>
          <tr>
            <th>Модель</th>
            <th className={styles.number}>Вопросов</th>
            <th className={styles.number}>Токенов на входе</th>
            <th className={styles.number}>Токенов на выходе</th>
          </tr>
        </thead>
        <tbody>
          {models.map((item) => (
            <tr key={item.model}>
              <td className={styles.code}>{item.model}</td>
              <td className={styles.number}>{formatNumber(item.questions)}</td>
              <td className={styles.number}>{formatNumber(item.input_tokens)}</td>
              <td className={styles.number}>{formatNumber(item.output_tokens)}</td>
            </tr>
          ))}
        </tbody>
      </Table>
    </section>
  );
}

function Companies({
  companies,
  tokens,
  price,
}: {
  companies: Spend["companies"];
  tokens: number;
  price: number | null;
}) {
  return (
    <section className={styles.tables} aria-labelledby="spend-companies">
      <h2 id="spend-companies" className={pageStyles.sectionTitle}>
        По компаниям
      </h2>
      <Table label="Расход по компаниям">
        <thead>
          <tr>
            <th>Компания</th>
            <th className={styles.number}>Вопросов</th>
            <th className={styles.number}>Токенов</th>
            <th className={styles.number}>Доля токенов</th>
            {price !== null ? <th className={styles.number}>≈ ₽</th> : null}
            <th className={styles.number}>Кредитов</th>
          </tr>
        </thead>
        <tbody>
          {companies.map((company) => (
            <tr key={company.tenant_id}>
              <td>
                {company.name}
                <span className={`${tableStyles.sub} ${styles.code}`}>
                  {company.company_code} · {company.ref}
                </span>
              </td>
              <td className={styles.number}>{formatNumber(company.questions)}</td>
              <td className={styles.number}>{formatNumber(company.tokens)}</td>
              <td className={styles.number}>{share(company.tokens, tokens)}</td>
              {price !== null ? (
                <td className={styles.number}>{formatRub((company.tokens / 1000) * price)}</td>
              ) : null}
              <td className={styles.number}>{formatNumber(company.credits)}</td>
            </tr>
          ))}
        </tbody>
      </Table>
    </section>
  );
}
