import { useMutation, useQuery } from "@tanstack/react-query";
import { useId, useState, type SubmitEvent } from "react";

import { api, unwrap, type Schemas } from "../api/client";
import { ApiError, errorMessage } from "../api/errors";
import { formatCalendarDate, formatNumber, plural } from "../lib/format";
import { formatPrice, tariffByCode, TARIFFS, type TariffCode } from "../lib/tariffs";
import { useDocumentTitle } from "../lib/title";
import { Section } from "../settings/common";
import { Badge } from "../ui/Badge";
import { Button } from "../ui/Button";
import { TextAreaField, TextField } from "../ui/Field";
import { Modal } from "../ui/Modal";
import { Notice } from "../ui/Notice";
import { Page, PageHeader } from "../ui/Page";
import pageStyles from "../ui/Page.module.css";
import { PageSpinner } from "../ui/Spinner";
import adminStyles from "./Admin.module.css";
import styles from "./Company.module.css";
import { ChoiceCards, type Choice } from "./CompanySettingsPage";

type Settings = Schemas["CompanySettingsResponse"];
type Usage = Schemas["UsageResponse"];

/** Как в схеме бэкенда (TariffRequest). */
const MAX_SEATS = 10_000;
const MAX_COMMENT = 1000;

/**
 * Тариф и лимит вопросов (ТЗ §7). Тариф и места меняет команда kronto:
 * администратор оставляет заявку, она приходит команде в Telegram. Оплата
 * картой и счётом — позже (§10).
 */
export function TariffPage() {
  useDocumentTitle("Тариф");
  const company = useQuery({
    // Тот же ключ, что у настроек компании: после их правки тариф уже свежий.
    queryKey: ["company"],
    queryFn: () => unwrap(api.GET("/api/v1/company")),
  });
  const usage = useQuery({ queryKey: ["usage"], queryFn: () => unwrap(api.GET("/api/v1/usage")) });
  const [requesting, setRequesting] = useState(false);

  return (
    <Page>
      <PageHeader
        label="управление"
        title="Тариф"
        description="Тариф, рабочие места и лимит вопросов на месяц."
      />
      {company.isPending || usage.isPending ? (
        <PageSpinner />
      ) : (
        <div className={styles.stack}>
          {company.isError ? (
            <Notice kind="error">{errorMessage(company.error)}</Notice>
          ) : (
            <PlanSection settings={company.data} onChange={() => setRequesting(true)} />
          )}
          {usage.isError ? (
            <Notice kind="error">{errorMessage(usage.error)}</Notice>
          ) : (
            <UsageSection usage={usage.data} />
          )}
          <p className={`muted ${styles.small}`}>
            Оплата картой и по счёту появится позже — пока тариф и места меняет команда kronto по
            вашей заявке.
          </p>
        </div>
      )}
      {requesting && company.data ? (
        <RequestDialog settings={company.data} onClose={() => setRequesting(false)} />
      ) : null}
    </Page>
  );
}

function PlanSection({ settings, onChange }: { settings: Settings; onChange: () => void }) {
  const plan = tariffByCode(settings.tariff);
  const { seats, members } = settings;
  const share = seats > 0 ? Math.min(1, members / seats) : 1;
  const full = members >= seats;
  return (
    <Section title={`Тариф «${plan?.name ?? settings.tariff}»`} description={plan?.summary}>
      <p>
        {plan?.price != null ? (
          <>
            <span className={`${styles.price} num`}>{formatPrice(plan.price)}</span>{" "}
            <span className="muted">за место в месяц</span>
          </>
        ) : (
          <span className={styles.price}>Цена по запросу</span>
        )}
      </p>
      <div className={styles.seats}>
        <p className={styles.seatsText}>
          <span>
            Занято <span className="num">{formatNumber(members)}</span> из{" "}
            <span className="num">{formatNumber(seats)}</span>{" "}
            {plural(seats, "места", "мест", "мест")}
          </span>
          {!full ? <span className="muted">свободно {formatNumber(seats - members)}</span> : null}
        </p>
        {/* Числа уже в тексте над полоской — она для глаза. */}
        <div className={adminStyles.progress} aria-hidden>
          <div
            className={`${adminStyles.progressBar} ${full ? adminStyles.progressWarn : ""}`}
            style={{ width: `${share * 100}%` }}
          />
        </div>
        {full ? (
          <p className={styles.warnText}>
            Свободных мест нет: новые сотрудники не смогут вступить по приглашению. Добавить места
            можно заявкой — кнопка «Сменить тариф».
          </p>
        ) : null}
      </div>
      {/* Внизу карточки: сначала что есть, потом — как поменять. */}
      <div className={styles.actions}>
        <Button size="sm" onClick={onChange}>
          Сменить тариф
        </Button>
      </div>
    </Section>
  );
}

function UsageSection({ usage }: { usage: Usage }) {
  const share = usage.pool > 0 ? Math.min(1, usage.used / usage.pool) : 1;
  const tone = usage.exhausted
    ? adminStyles.progressError
    : usage.warning
      ? adminStyles.progressWarn
      : "";
  return (
    <Section
      title="Лимит вопросов"
      description="Кредиты — общий пул компании на месяц: столько-то на каждое рабочее место. Один вопрос обычно стоит один кредит, длинный — несколько."
    >
      {usage.exhausted ? (
        <Notice kind="error" title="Лимит исчерпан">
          Сотрудники не могут задавать вопросы до {formatCalendarDate(usage.period_end)}. Чтобы
          увеличить лимит, добавьте места — заявкой «Сменить тариф».
        </Notice>
      ) : usage.warning ? (
        <Notice kind="warn" title="Лимит скоро закончится">
          Израсходовано {Math.round(share * 100)} % пула.
        </Notice>
      ) : null}
      <div>
        <p className="mono muted">
          {formatCalendarDate(usage.period_start)} — {formatCalendarDate(usage.period_end, -1)}
        </p>
        <p className={`${styles.used} num`}>
          {formatNumber(usage.used)} <span className="muted">из {formatNumber(usage.pool)}</span>
        </p>
        <div
          className={adminStyles.progress}
          role="progressbar"
          aria-label="Израсходовано кредитов"
          aria-valuemin={0}
          aria-valuemax={usage.pool}
          aria-valuenow={usage.used}
        >
          <div
            className={`${adminStyles.progressBar} ${tone}`}
            style={{ width: `${share * 100}%` }}
          />
        </div>
      </div>
      <div className={adminStyles.stats}>
        <Stat value={formatNumber(usage.remaining)} label="осталось кредитов" />
        <Stat value={formatNumber(usage.credits_per_seat)} label="кредитов на место в месяц" />
      </div>
    </Section>
  );
}

function Stat({ value, label }: { value: string; label: string }) {
  return (
    <div className={adminStyles.stat}>
      <span className={adminStyles.statValue}>{value}</span>
      <span className={adminStyles.statLabel}>{label}</span>
    </div>
  );
}

function tariffChoices(current: TariffCode): Choice<TariffCode>[] {
  return TARIFFS.map((tariff) => ({
    value: tariff.code,
    title: tariff.name,
    aside: tariff.code === current ? <Badge>сейчас</Badge> : null,
    text: (
      <>
        <span className={styles.choicePrice}>
          {tariff.price === null
            ? "Цена по запросу"
            : `${formatPrice(tariff.price)} за место в месяц`}
        </span>
        <span>{tariff.summary}</span>
      </>
    ),
  }));
}

/**
 * Заявка на смену тарифа или числа мест. Окно монтируется на время
 * заявки — поля и итог не переживают закрытие.
 */
function RequestDialog({ settings, onClose }: { settings: Settings; onClose: () => void }) {
  const formId = useId();
  const [tariff, setTariff] = useState<TariffCode>(settings.tariff);
  const [seats, setSeats] = useState("");
  const [comment, setComment] = useState("");
  const [problem, setProblem] = useState<string | null>(null);
  const [seatsError, setSeatsError] = useState<string | null>(null);
  const send = useMutation({
    mutationFn: (body: Schemas["TariffRequest"]) =>
      unwrap(api.POST("/api/v1/company/tariff-request", { body })),
  });

  function submit(event: SubmitEvent) {
    event.preventDefault();
    setProblem(null);
    setSeatsError(null);
    const count = seats.trim() ? Number(seats) : null;
    if (count !== null && (!Number.isInteger(count) || count < 1 || count > MAX_SEATS)) {
      setSeatsError(`Укажите целое число от 1 до ${formatNumber(MAX_SEATS)}.`);
      return;
    }
    const note = comment.trim();
    if (tariff === settings.tariff && (count === null || count === settings.seats) && !note) {
      setProblem("Выберите другой тариф или укажите, сколько мест нужно.");
      return;
    }
    send.mutate({
      tariff,
      ...(count !== null ? { seats: count } : {}),
      ...(note ? { comment: note } : {}),
    });
  }

  // Заявок не больше пяти в сутки: общий текст 429 тут непонятен.
  const sendError =
    send.error instanceof ApiError && send.error.status === 429
      ? "Заявок на сегодня уже много — команда kronto их получила и свяжется с вами. Новую можно отправить завтра."
      : send.isError
        ? errorMessage(send.error)
        : null;

  const done = send.isSuccess;
  return (
    <Modal
      open
      onOpenChange={(open) => !open && !send.isPending && onClose()}
      title={done ? "Заявка отправлена" : "Сменить тариф"}
      description={
        done
          ? undefined
          : "Заявка уйдёт команде kronto: она свяжется с вами и поменяет тариф или число мест."
      }
      footer={
        done ? (
          <Button size="sm" onClick={onClose}>
            Готово
          </Button>
        ) : (
          <>
            <Button variant="ghost" size="sm" onClick={onClose} disabled={send.isPending}>
              Отмена
            </Button>
            <Button type="submit" form={formId} size="sm" busy={send.isPending}>
              Отправить заявку
            </Button>
          </>
        )
      }
    >
      {done ? (
        <Notice kind="ok">
          Команда kronto свяжется с вами, чтобы обсудить условия, и сама поменяет тариф.
        </Notice>
      ) : (
        <form id={formId} className={pageStyles.form} onSubmit={submit} noValidate>
          {sendError ? <Notice kind="error">{sendError}</Notice> : null}
          <ChoiceCards
            label="Тариф"
            value={tariff}
            options={tariffChoices(settings.tariff)}
            onChange={(value) => {
              setTariff(value);
              setProblem(null);
            }}
          />
          <TextField
            label="Сколько мест нужно"
            optional
            type="number"
            inputMode="numeric"
            min={1}
            max={MAX_SEATS}
            value={seats}
            onChange={(e) => {
              setSeats(e.target.value);
              setProblem(null);
              setSeatsError(null);
            }}
            hint={`Сейчас оплачено ${formatNumber(settings.seats)}, занято ${formatNumber(settings.members)}. Пусто — число мест не меняется.`}
            error={seatsError}
          />
          <TextAreaField
            label="Комментарий"
            optional
            maxLength={MAX_COMMENT}
            rows={3}
            value={comment}
            onChange={(e) => {
              setComment(e.target.value);
              setProblem(null);
            }}
            placeholder="Например, с какого числа нужен новый тариф и как удобнее связаться"
          />
          {problem ? <Notice kind="warn">{problem}</Notice> : null}
        </form>
      )}
    </Modal>
  );
}
