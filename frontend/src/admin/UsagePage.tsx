import { useQuery } from "@tanstack/react-query";

import { api, unwrap, type Schemas } from "../api/client";
import { errorMessage } from "../api/errors";
import { formatDate, formatNumber } from "../lib/format";
import { Notice } from "../ui/Notice";
import { Page, PageHeader } from "../ui/Page";
import pageStyles from "../ui/Page.module.css";
import { PageSpinner } from "../ui/Spinner";
import styles from "./Admin.module.css";

export function UsagePage() {
  const usage = useQuery({ queryKey: ["usage"], queryFn: () => unwrap(api.GET("/api/v1/usage")) });

  return (
    <Page>
      <PageHeader
        label="управление"
        title="Лимит вопросов"
        description="Кредиты — общий пул компании на месяц: столько-то на каждое рабочее место. Один вопрос обычно стоит один кредит, длинный — несколько."
      />
      {usage.isPending ? (
        <PageSpinner />
      ) : usage.isError ? (
        <Notice kind="error">{errorMessage(usage.error)}</Notice>
      ) : (
        <UsageView usage={usage.data} />
      )}
    </Page>
  );
}

function UsageView({ usage }: { usage: Schemas["UsageResponse"] }) {
  const share = usage.pool > 0 ? Math.min(1, usage.used / usage.pool) : 1;
  const tone = usage.exhausted ? styles.progressError : share >= 0.8 ? styles.progressWarn : "";
  return (
    <div className={pageStyles.stack}>
      {usage.exhausted ? (
        <Notice kind="error" title="Лимит исчерпан">
          Сотрудники не могут задавать вопросы до {formatDate(usage.period_end)}. Чтобы увеличить
          лимит, добавьте рабочие места — напишите нам.
        </Notice>
      ) : share >= 0.8 ? (
        <Notice kind="warn" title="Лимит скоро закончится">
          Израсходовано {Math.round(share * 100)} % пула.
        </Notice>
      ) : null}
      <div className={pageStyles.card}>
        <p className="mono muted">
          {formatDate(usage.period_start)} — {formatDate(usage.period_end)}
        </p>
        <p
          style={{ fontSize: "var(--fs-h3)", fontWeight: 500, margin: "8px 0 16px" }}
          className="num"
        >
          {formatNumber(usage.used)} <span className="muted">из {formatNumber(usage.pool)}</span>
        </p>
        <div
          className={styles.progress}
          role="progressbar"
          aria-label="Израсходовано кредитов"
          aria-valuemin={0}
          aria-valuemax={usage.pool}
          aria-valuenow={usage.used}
        >
          <div className={`${styles.progressBar} ${tone}`} style={{ width: `${share * 100}%` }} />
        </div>
      </div>
      <div className={styles.stats}>
        <Stat value={formatNumber(usage.remaining)} label="осталось кредитов" />
        <Stat value={formatNumber(usage.seats)} label="рабочих мест" />
        <Stat value={formatNumber(usage.credits_per_seat)} label="кредитов на место в месяц" />
      </div>
    </div>
  );
}

function Stat({ value, label }: { value: string; label: string }) {
  return (
    <div className={styles.stat}>
      <span className={styles.statValue}>{value}</span>
      <span className={styles.statLabel}>{label}</span>
    </div>
  );
}
