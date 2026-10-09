import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useId, useState, type SubmitEvent } from "react";

import { api, unwrap, type Schemas } from "../api/client";
import { ApiError, errorMessage } from "../api/errors";
import { averageQuestionNote, credits, formatKopecks } from "../lib/credits";
import { formatCalendarDate, formatDate, formatNumber, plural } from "../lib/format";
import { BILLING_KEY, type Method } from "../lib/billing";
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
import { Table } from "../ui/Table";
import adminStyles from "./Admin.module.css";
import { DocumentsSection, IssuedNotice, SubscriptionSection } from "./BillingSections";
import styles from "./Company.module.css";
import { ChoiceCards, type Choice } from "./CompanySettingsPage";

type Settings = Schemas["CompanySettingsResponse"];
type Usage = Schemas["UsageResponse"];
type Pack = Schemas["CreditPackResponse"];
type Order = Schemas["CreditOrderResponse"];

/** Как в схеме бэкенда (TariffRequest). */
const MAX_SEATS = 10_000;
const MAX_COMMENT = 1000;

const ORDERS_KEY = ["credits", "orders"] as const;
const BUY_OR_ADD = "Купите пакет кредитов или добавьте места.";

/**
 * Тариф и кредиты (ТЗ §7, решение владельца 09.10). Тариф и места меняет
 * команда kronto по заявке. Кредиты: месячный пул и купленные пакеты;
 * пакет администратор заказывает здесь. Цены пакетов — с бэкенда (GET
 * /credits/packs).
 *
 * Оплата (GET /billing): пока банк не подключён (enabled=false), пакет
 * оплачивается по счёту от команды, и она же зачисляет кредиты. С банком —
 * подписка счётом или картой, счета и акты для скачивания, пакет — счётом
 * или ссылкой с чеком.
 */
export function TariffPage() {
  useDocumentTitle("Тариф");
  const company = useQuery({
    // Тот же ключ, что у настроек компании: после их правки тариф уже свежий.
    queryKey: ["company"],
    queryFn: () => unwrap(api.GET("/api/v1/company")),
  });
  const usage = useQuery({ queryKey: ["usage"], queryFn: () => unwrap(api.GET("/api/v1/usage")) });
  const billing = useQuery({
    queryKey: BILLING_KEY,
    queryFn: () => unwrap(api.GET("/api/v1/billing")),
  });
  const [requesting, setRequesting] = useState(false);
  const enabled = billing.data?.enabled === true;

  return (
    <Page>
      <PageHeader label="управление" title="Тариф" description="Тариф, рабочие места и кредиты." />
      {company.isPending || usage.isPending ? (
        <PageSpinner />
      ) : (
        <div className={styles.stack}>
          {company.isError ? (
            <Notice kind="error">{errorMessage(company.error)}</Notice>
          ) : (
            <PlanSection settings={company.data} onChange={() => setRequesting(true)} />
          )}
          {billing.data && enabled ? <SubscriptionSection billing={billing.data} /> : null}
          {usage.isError ? (
            <Notice kind="error">{errorMessage(usage.error)}</Notice>
          ) : (
            <UsageSection usage={usage.data} />
          )}
          <PacksSection enabled={enabled} />
          {billing.data && enabled ? <DocumentsSection billing={billing.data} /> : null}
          <p className={`muted ${styles.small}`}>
            {enabled
              ? "Тариф и места меняет команда kronto по вашей заявке. Подписка и пакеты оплачиваются счётом для юрлица или ИП либо картой и через СБП с чеком; акт за месяц — на этой странице и на почте для документов."
              : "Тариф и места меняет команда kronto по вашей заявке. Пакеты кредитов пока оплачиваются по счёту, оплата картой появится позже."}
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
  const tone = usage.stopped
    ? adminStyles.progressError
    : usage.warning
      ? adminStyles.progressWarn
      : "";
  const renews = formatCalendarDate(usage.period_end);
  return (
    <Section
      title="Кредиты"
      description={`Кредиты — общий пул компании на месяц: ${credits(usage.credits_per_seat)} на каждое рабочее место. Сверх пула расходуются купленные пакеты. ${averageQuestionNote(usage.avg_credits_per_question)}`}
    >
      {usage.stopped ? (
        <Notice kind="error" title="Кредиты закончились">
          Сотрудники не могут задавать вопросы до {renews}. {BUY_OR_ADD}
        </Notice>
      ) : usage.exhausted ? (
        <Notice kind="warn" title="Месячный пул израсходован">
          Вопросы списываются с купленных кредитов. Пул обновится {renews}.
        </Notice>
      ) : usage.warning ? (
        <Notice kind="warn" title="Кредиты скоро закончатся">
          Израсходовано {Math.round(share * 100)} % месячного пула. {BUY_OR_ADD}
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
          aria-label="Израсходовано кредитов месячного пула"
          aria-valuemin={0}
          aria-valuemax={usage.pool}
          aria-valuenow={usage.used}
        >
          <div
            className={`${adminStyles.progressBar} ${tone}`}
            style={{ width: `${share * 100}%` }}
          />
        </div>
        <p className={`muted ${styles.small} ${styles.after}`}>Пул обновится {renews}.</p>
      </div>
      <div className={adminStyles.stats}>
        <Stat value={formatNumber(usage.remaining)} label="осталось в пуле" />
        <Stat value={formatNumber(usage.credits_per_seat)} label="кредитов на место в месяц" />
        <Stat value={formatNumber(usage.purchased)} label="купленных кредитов" />
      </div>
      <p className={`muted ${styles.small}`}>
        {usage.purchased > 0 && usage.purchased_expires_at
          ? `Ближайшее сгорание: ${credits(usage.purchased_expiring)} ${plural(usage.purchased_expiring, "сгорит", "сгорят", "сгорят")} ${formatCalendarDate(usage.purchased_expires_at)}.`
          : "Купленных кредитов нет."}
      </p>
    </Section>
  );
}

/**
 * Пакеты кредитов: цены и срок жизни — с бэкенда. «Купить» создаёт заказ.
 * Без банка он ждёт оплаты по счёту от команды; с банком — сразу счёт или
 * ссылка на оплату картой, кредиты зачисляются после оплаты сами.
 */
function PacksSection({ enabled }: { enabled: boolean }) {
  const packs = useQuery({
    queryKey: ["credits", "packs"],
    queryFn: () => unwrap(api.GET("/api/v1/credits/packs")),
  });
  const orders = useQuery({
    queryKey: ORDERS_KEY,
    queryFn: () => unwrap(api.GET("/api/v1/credits/orders")),
  });
  const [buying, setBuying] = useState<Pack | null>(null);
  const months = packs.data?.[0]?.valid_months;
  return (
    <Section
      title="Пакеты кредитов"
      description={
        months
          ? `Расходуются после месячного пула и действуют ${months} ${plural(months, "месяц", "месяца", "месяцев")} с зачисления; первыми списываются те, что раньше сгорают. ${
              enabled
                ? "Оплата — счётом для юрлица или ИП либо картой и через СБП с чеком; кредиты зачисляются сразу после оплаты."
                : "Оплата — по счёту: команда kronto пришлёт его и зачислит кредиты после оплаты."
            }`
          : undefined
      }
    >
      {packs.isPending ? (
        <PageSpinner />
      ) : packs.isError ? (
        <Notice kind="error">{errorMessage(packs.error)}</Notice>
      ) : (
        <div className={adminStyles.stats}>
          {packs.data.map((pack) => (
            <div key={pack.code} className={adminStyles.stat}>
              <span className={adminStyles.statValue}>{credits(pack.credits)}</span>
              <span className="num">{formatKopecks(pack.price_kopecks)}</span>
              <span className={adminStyles.statLabel}>
                {formatKopecks(Math.round(pack.price_kopecks / pack.credits))} за кредит
              </span>
              <div>
                <Button
                  size="sm"
                  onClick={() => setBuying(pack)}
                  aria-label={`Купить ${credits(pack.credits)}`}
                >
                  Купить
                </Button>
              </div>
            </div>
          ))}
        </div>
      )}
      {orders.isPending ? null : orders.isError ? (
        <Notice kind="error">{errorMessage(orders.error)}</Notice>
      ) : (
        <OrdersTable orders={orders.data} />
      )}
      {buying ? (
        <BuyDialog pack={buying} enabled={enabled} onClose={() => setBuying(null)} />
      ) : null}
    </Section>
  );
}

const ORDER_STATUS: Record<Order["status"], { label: string; tone: "warn" | "ok" | "muted" }> = {
  awaiting_payment: { label: "Ждёт оплаты", tone: "warn" },
  paid: { label: "Оплачен", tone: "ok" },
  cancelled: { label: "Отменён", tone: "muted" },
};

function OrdersTable({ orders }: { orders: Order[] }) {
  if (orders.length === 0) return <p className={`muted ${styles.small}`}>Заказов пока нет</p>;
  return (
    <Table label="Заказы">
      <thead>
        <tr>
          <th>Заказ</th>
          <th>Пакет</th>
          <th>Сумма</th>
          <th>Статус</th>
          <th>Создан</th>
        </tr>
      </thead>
      <tbody>
        {orders.map((order) => {
          const status = ORDER_STATUS[order.status];
          return (
            <tr key={order.id}>
              <td>№ {order.number}</td>
              <td>{credits(order.credits)}</td>
              <td className="num">{formatKopecks(order.amount_kopecks)}</td>
              <td>
                <Badge tone={status.tone}>{status.label}</Badge>
              </td>
              <td>{formatDate(order.created_at)}</td>
            </tr>
          );
        })}
      </tbody>
    </Table>
  );
}

const PACK_METHODS: Choice<Method>[] = [
  {
    value: "invoice",
    title: "Счёт для юрлица или ИП",
    text: "Оплата с расчётного счёта; нужны реквизиты компании.",
  },
  {
    value: "card",
    title: "Картой или СБП",
    text: "Сразу по ссылке, чек придёт на почту.",
  },
];

/** Подтверждение заказа. Окно монтируется на время покупки. */
function BuyDialog({
  pack,
  enabled,
  onClose,
}: {
  pack: Pack;
  enabled: boolean;
  onClose: () => void;
}) {
  const queryClient = useQueryClient();
  const [method, setMethod] = useState<Method>("invoice");
  const order = useMutation({
    mutationFn: () =>
      unwrap(
        api.POST("/api/v1/credits/orders", {
          // Без банка — прежнее тело: способа оплаты нет, счёт пришлёт команда
          // (на сервере по умолчанию invoice).
          body: (enabled
            ? { pack: pack.code, payment_method: method }
            : { pack: pack.code }) as Schemas["CreditOrderRequest"],
        }),
      ),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ORDERS_KEY });
      void queryClient.invalidateQueries({ queryKey: BILLING_KEY });
    },
  });
  const done = order.data;
  const months = plural(pack.valid_months, "месяц", "месяца", "месяцев");
  return (
    <Modal
      open
      onOpenChange={(open) => !open && !order.isPending && onClose()}
      title="Купить пакет кредитов"
      description={
        done ? undefined : `${credits(pack.credits)} за ${formatKopecks(pack.price_kopecks)}`
      }
      footer={
        done ? (
          <Button size="sm" onClick={onClose}>
            Готово
          </Button>
        ) : (
          <>
            <Button variant="ghost" size="sm" onClick={onClose} disabled={order.isPending}>
              Отмена
            </Button>
            <Button size="sm" busy={order.isPending} onClick={() => order.mutate()}>
              Заказать
            </Button>
          </>
        )
      }
    >
      {done && done.invoice_id ? (
        <PaidByBank order={done} />
      ) : done ? (
        <Notice kind="ok" title={`Заказ № ${done.number} ждёт оплаты`}>
          Команда kronto пришлёт счёт на {formatKopecks(done.amount_kopecks)}. Кредиты появятся
          после оплаты — администраторам придёт уведомление.
        </Notice>
      ) : (
        <>
          {order.isError ? <Notice kind="error">{errorMessage(order.error)}</Notice> : null}
          {enabled ? (
            <>
              <ChoiceCards
                label="Способ оплаты"
                value={method}
                options={PACK_METHODS}
                onChange={setMethod}
              />
              <p>
                Кредиты зачислятся сразу после оплаты, расходуются после месячного пула и действуют{" "}
                {pack.valid_months} {months} с зачисления.
              </p>
            </>
          ) : (
            <p>
              Создадим заказ, команда kronto пришлёт счёт. Кредиты расходуются после месячного пула
              и действуют {pack.valid_months} {months} с зачисления.
            </p>
          )}
        </>
      )}
    </Modal>
  );
}

/** Заказ со счётом или ссылкой банка: что делать дальше. */
function PaidByBank({ order }: { order: Order }) {
  if (order.payment_url) {
    return (
      <IssuedNotice
        invoice={{
          id: order.invoice_id ?? order.id,
          number: `№ ${order.number}`,
          kind: "credits",
          status: "awaiting_payment",
          payment_method: "card",
          amount_kopecks: order.amount_kopecks,
          title: "",
          purpose: "",
          period_start: null,
          period_end: null,
          due_date: null,
          payment_url: order.payment_url,
          has_pdf: false,
          created_at: order.created_at,
          paid_at: null,
        }}
      />
    );
  }
  return (
    <Notice kind="ok" title={`Заказ № ${order.number}: счёт выставлен`}>
      Счёт на {formatKopecks(order.amount_kopecks)} — в списке «Счета и акты» на этой странице.
      Кредиты зачислятся, как только придёт оплата.
    </Notice>
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
