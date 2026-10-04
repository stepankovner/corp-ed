import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Building2 } from "lucide-react";
import { useId, useMemo, useState, type SubmitEvent } from "react";

import adminStyles from "../admin/Admin.module.css";
import companyStyles from "../admin/Company.module.css";
import { api, unwrap, type Schemas } from "../api/client";
import { ApiError, errorMessage } from "../api/errors";
import { useMe } from "../auth/context";
import {
  formatCalendarDate,
  formatDateTime,
  formatNumber,
  formatRelative,
  plural,
} from "../lib/format";
import { priceLabel, tariffByCode, TARIFFS, type TariffCode } from "../lib/tariffs";
import { useDocumentTitle } from "../lib/title";
import { Badge } from "../ui/Badge";
import { Button } from "../ui/Button";
import { Checkbox, SelectField, TextField } from "../ui/Field";
import fieldStyles from "../ui/Field.module.css";
import { Modal } from "../ui/Modal";
import { Notice } from "../ui/Notice";
import { EmptyState } from "../ui/Page";
import pageStyles from "../ui/Page.module.css";
import { SkeletonList } from "../ui/Skeleton";
import { Switch } from "../ui/Switch";
import { Table } from "../ui/Table";
import tableStyles from "../ui/Table.module.css";
import { useToast } from "../ui/useToast";
import styles from "./Companies.module.css";
import { STAFF_KEY } from "./keys";

type Company = Schemas["StaffCompanyResponse"];
type Update = Schemas["StaffCompanyUpdate"];
/** Только изменённые поля; confirm — только когда команда согласилась остановить вопросы. */
type Change = Omit<Update, "confirm"> & { confirm?: true };

/** Как на бэкенде (StaffCompanyUpdate). */
const MAX_SEATS = 10_000;
/** Пилот подсвечиваем за неделю до конца — как счётчик в шапке панели. */
const PILOT_WARN_DAYS = 7;
/** Как порог предупреждения о лимите вопросов у администратора (warn_at_percent). */
const POOL_WARN_PERCENT = 80;
const DAY_MS = 86_400_000;

/** Сегодня по часам браузера — «ГГГГ-ММ-ДД», как у поля даты и pilot_until. */
function today(): string {
  const now = new Date();
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())}`;
}

/** Дней от одной календарной даты до другой. */
function daysBetween(from: string, to: string): number {
  return Math.round((Date.parse(`${to}T00:00:00Z`) - Date.parse(`${from}T00:00:00Z`)) / DAY_MS);
}

/** Доля потраченного пула: с порога — предупреждение, весь пул — ошибка. */
function shareTone(share: number): string {
  if (share >= 100) return styles.error ?? "";
  return share >= POOL_WARN_PERCENT ? (styles.warn ?? "") : "";
}

function tariffName(code: TariffCode): string {
  return tariffByCode(code)?.name ?? code;
}

function matches(company: Company, needle: string): boolean {
  return [company.name, company.company_code, ...company.admins].some((value) =>
    value.toLowerCase().includes(needle),
  );
}

function isStopPool(error: unknown): error is ApiError {
  return error instanceof ApiError && error.code === "seats_stop_pool";
}

function savedMessage(name: string, change: Change): string {
  if (change.is_active === false) return `На паузе: ${name}`;
  if (change.is_active === true) return `Снова работает: ${name}`;
  return `Сохранено: ${name}`;
}

/**
 * Компании (ТЗ §9): тариф, места, срок пилота и пауза — то, что раньше
 * команда меняла командами cli set-tariff, set-seats, suspend-tenant.
 * Содержимого компаний панель не показывает: только счётчики и почту
 * администраторов, чтобы им написать.
 */
export function CompaniesTab() {
  useDocumentTitle("Компании");
  const companies = useQuery({
    queryKey: [...STAFF_KEY, "companies"],
    queryFn: () => unwrap(api.GET("/api/v1/staff/companies")),
  });
  const [search, setSearch] = useState("");
  const [editing, setEditing] = useState<Company | null>(null);
  const needle = search.trim().toLowerCase();
  const visible = useMemo(
    () => (companies.data ?? []).filter((company) => !needle || matches(company, needle)),
    [companies.data, needle],
  );
  const now = today();

  return (
    <>
      {companies.isPending ? (
        <SkeletonList label="Загрузка компаний" />
      ) : companies.isError ? (
        <Notice kind="error">{errorMessage(companies.error)}</Notice>
      ) : companies.data.length === 0 ? (
        <EmptyState icon={<Building2 size={32} aria-hidden />} title="Компаний пока нет">
          <p>Компания появится, когда вы одобрите заявку на вкладке «Заявки».</p>
        </EmptyState>
      ) : (
        <>
          <div className={adminStyles.toolbar}>
            <span className={adminStyles.searchWrap}>
              <input
                className={adminStyles.search}
                type="search"
                placeholder="Название, код, почта администратора"
                aria-label="Поиск по названию, коду и почте администратора"
                value={search}
                onChange={(e) => setSearch(e.target.value)}
              />
            </span>
          </div>
          {visible.length === 0 ? (
            <p className="muted">Ничего не найдено.</p>
          ) : (
            <Table label="Компании">
              <thead>
                <tr>
                  <th>Компания</th>
                  <th>Тариф</th>
                  <th>Места</th>
                  <th>Кредиты за месяц</th>
                  <th>Пилот</th>
                  <th>Состояние</th>
                  <th>Последний вопрос</th>
                  <th className={tableStyles.actions}>
                    <span className="visually-hidden">Действия</span>
                  </th>
                </tr>
              </thead>
              <tbody>
                {visible.map((company) => (
                  <CompanyRow
                    key={company.id}
                    company={company}
                    now={now}
                    onEdit={() => setEditing(company)}
                  />
                ))}
              </tbody>
            </Table>
          )}
        </>
      )}
      {editing ? <CompanyDialog company={editing} onClose={() => setEditing(null)} /> : null}
    </>
  );
}

function CompanyRow({
  company,
  now,
  onEdit,
}: {
  company: Company;
  now: string;
  onEdit: () => void;
}) {
  const share = company.pool > 0 ? Math.round((company.credits_used / company.pool) * 100) : null;
  return (
    <tr>
      <td>
        <div className={styles.company}>
          <span className={styles.name}>{company.name}</span>
          <span className={`mono ${styles.code}`}>{company.company_code}</span>
          <span className={tableStyles.sub}>
            {company.admins.length ? company.admins.join(", ") : "администраторов нет"}
          </span>
        </div>
      </td>
      <td className={tableStyles.nowrap}>{tariffName(company.tariff)}</td>
      <td className={tableStyles.nowrap}>
        <span className="num">
          {formatNumber(company.members)} из {formatNumber(company.seats)}
        </span>
        {company.pending ? (
          <span className={tableStyles.sub}>ждут: {formatNumber(company.pending)}</span>
        ) : null}
      </td>
      <td className={tableStyles.nowrap}>
        <span className="num">
          {formatNumber(company.credits_used)} из {formatNumber(company.pool)}
        </span>
        {share !== null ? (
          <span className={`${styles.share} ${shareTone(share)}`}>{share} %</span>
        ) : null}
      </td>
      {/* nowrap — у ячейки: на телефоне таблица его снимает, и дата переносится. */}
      <td className={tableStyles.nowrap}>
        {company.pilot_until ? (
          <div className={styles.cell}>
            <span>до {formatCalendarDate(company.pilot_until)}</span>
            <PilotBadge until={company.pilot_until} now={now} />
          </div>
        ) : (
          "—"
        )}
      </td>
      <td>
        {company.is_active ? (
          <Badge tone="ok">работает</Badge>
        ) : (
          <Badge tone="error">на паузе</Badge>
        )}
      </td>
      <td
        className={tableStyles.nowrap}
        title={company.last_question_at ? formatDateTime(company.last_question_at) : undefined}
      >
        {company.last_question_at ? formatRelative(company.last_question_at) : "не было"}
        <span className={tableStyles.sub}>за месяц: {formatNumber(company.questions_month)}</span>
      </td>
      <td className={tableStyles.actions}>
        <Button variant="ghost" size="xs" aria-label={`Изменить: ${company.name}`} onClick={onEdit}>
          Изменить
        </Button>
      </td>
    </tr>
  );
}

/** pilot_until — последний день пилота: в этот день он ещё идёт. */
function PilotBadge({ until, now }: { until: string; now: string }) {
  const left = daysBetween(now, until);
  if (left < 0) return <Badge tone="error">закончился</Badge>;
  if (left === 0) return <Badge tone="warn">последний день</Badge>;
  if (left > PILOT_WARN_DAYS) return null;
  return (
    <Badge tone="warn">
      {plural(left, "остался", "осталось", "осталось")} {left} {plural(left, "день", "дня", "дней")}
    </Badge>
  );
}

/**
 * Правка компании. Уходят только изменённые поля. Если новые места
 * дают пул меньше уже потраченного, сервер отвечает 409 seats_stop_pool —
 * сохраняем повторно с confirm, только когда команда это явно отметила.
 */
function CompanyDialog({ company, onClose }: { company: Company; onClose: () => void }) {
  const formId = useId();
  const pilotHintId = useId();
  const queryClient = useQueryClient();
  const toast = useToast();
  const me = useMe();
  // Свою компанию сервер не приостановит (own_company): токен этой
  // компании перестал бы приниматься, и панель закрылась бы посреди работы.
  const own = me.company?.tenant_id === company.id;
  const [tariff, setTariff] = useState<TariffCode>(company.tariff);
  const [seats, setSeats] = useState(String(company.seats));
  const [pilot, setPilot] = useState(company.pilot_until ?? "");
  const [active, setActive] = useState(company.is_active);
  const [seatsError, setSeatsError] = useState<string | null>(null);
  // Ответ сервера на места, при которых пул меньше потраченного, и согласие команды.
  const [stop, setStop] = useState<string | null>(null);
  const [force, setForce] = useState(false);

  const save = useMutation({
    mutationFn: (change: Change) =>
      unwrap(
        api.PATCH("/api/v1/staff/companies/{tenant_id}", {
          params: { path: { tenant_id: company.id } },
          // confirm в схеме обязателен (есть значение по умолчанию), а шлём его
          // только с согласием: «что прислано, то и меняется».
          body: change as Update,
        }),
      ),
    onSuccess: async (_, change) => {
      await queryClient.invalidateQueries({ queryKey: STAFF_KEY });
      toast.show(savedMessage(company.name, change));
      onClose();
    },
    onError: (error) => {
      if (isStopPool(error)) setStop(error.message);
    },
  });

  const count = Number(seats);
  const seatsValid =
    seats.trim() !== "" && Number.isInteger(count) && count >= 1 && count <= MAX_SEATS;
  const seatsChanged = seatsValid
    ? count !== company.seats
    : seats.trim() !== String(company.seats);
  const change: Change = {
    ...(tariff !== company.tariff ? { tariff } : {}),
    ...(seatsChanged ? { seats: count } : {}),
    ...(pilot !== (company.pilot_until ?? "") ? { pilot_until: pilot || null } : {}),
    ...(active !== company.is_active ? { is_active: active } : {}),
  };
  const dirty = Object.keys(change).length > 0;
  const pausing = company.is_active && !active;
  const perSeat = company.seats > 0 ? Math.round(company.pool / company.seats) : 0;

  function submit(event: SubmitEvent) {
    event.preventDefault();
    if (seatsChanged && !seatsValid) {
      setSeatsError(`Укажите целое число от 1 до ${formatNumber(MAX_SEATS)}.`);
      return;
    }
    if (!dirty || (stop !== null && !force)) return;
    save.mutate(stop !== null ? { ...change, confirm: true } : change);
  }

  return (
    <Modal
      open
      onOpenChange={(open) => !open && !save.isPending && onClose()}
      title={company.name}
      description={
        <>
          Код <span className="mono">{company.company_code}</span>. Изменения попадут в журнал
          действий компании.
        </>
      }
      footer={
        <>
          <Button variant="ghost" size="sm" onClick={onClose} disabled={save.isPending}>
            Отмена
          </Button>
          <Button
            type="submit"
            form={formId}
            size="sm"
            variant={pausing ? "danger" : "dark"}
            busy={save.isPending}
            disabled={!dirty || (stop !== null && !force)}
          >
            {pausing ? "Приостановить" : "Сохранить"}
          </Button>
        </>
      }
    >
      <form id={formId} className={pageStyles.form} onSubmit={submit} noValidate>
        {save.isError && !isStopPool(save.error) ? (
          <Notice kind="error">{errorMessage(save.error)}</Notice>
        ) : null}
        <dl className={`${adminStyles.kv} ${styles.summary}`}>
          <dt>Администраторы</dt>
          <dd>
            {company.admins.length ? (
              <span className={styles.contacts}>
                {company.admins.map((email) => (
                  <a key={email} href={`mailto:${email}`}>
                    {email}
                  </a>
                ))}
              </span>
            ) : (
              "нет"
            )}
          </dd>
          <dt>Сотрудники</dt>
          <dd>
            {formatNumber(company.members)} из {formatNumber(company.seats)}{" "}
            {plural(company.seats, "места", "мест", "мест")}
            {company.pending ? `, ждут одобрения: ${formatNumber(company.pending)}` : ""}
          </dd>
          <dt>Кредиты</dt>
          <dd>
            {formatNumber(company.credits_used)} из {formatNumber(company.pool)} за месяц
          </dd>
          <dt>Источники</dt>
          <dd>
            документов: {formatNumber(company.documents)}, подключений:{" "}
            {formatNumber(company.connectors)}
          </dd>
        </dl>
        <SelectField
          label="Тариф"
          value={tariff}
          onChange={(e) => setTariff(e.target.value as TariffCode)}
        >
          {TARIFFS.map((item) => (
            <option key={item.code} value={item.code}>
              {item.name} — {priceLabel(item)}
            </option>
          ))}
        </SelectField>
        <TextField
          label="Рабочих мест"
          type="number"
          inputMode="numeric"
          required
          min={1}
          max={MAX_SEATS}
          value={seats}
          onChange={(e) => {
            setSeats(e.target.value);
            setSeatsError(null);
            // Другое число мест — и последствия другие: спросим сервер заново.
            setStop(null);
            setForce(false);
          }}
          hint={
            seatsValid && seatsChanged
              ? `Пул станет ${formatNumber(count * perSeat)} кредитов в месяц (${formatNumber(perSeat)} на место); потрачено ${formatNumber(company.credits_used)}.`
              : `${formatNumber(perSeat)} кредитов на место в месяц.`
          }
          error={seatsError}
        />
        {seatsValid && count < company.members ? (
          <p className={companyStyles.warnText}>
            Мест меньше, чем сотрудников ({formatNumber(company.members)}): новые не смогут
            вступить, пока лишних не уберут из компании.
          </p>
        ) : null}
        {stop !== null ? (
          <>
            <Notice kind="warn" title="Вопросы остановятся">
              {stop.charAt(0).toUpperCase() + stop.slice(1)}
            </Notice>
            <Checkbox
              label="Всё равно сохранить — вопросы остановятся до конца месяца"
              checked={force}
              onChange={(e) => setForce(e.target.checked)}
            />
          </>
        ) : null}
        <div>
          <div className={styles.pilotRow}>
            <TextField
              label="Пилот до"
              optional
              type="date"
              value={pilot}
              onChange={(e) => setPilot(e.target.value)}
              aria-describedby={pilotHintId}
            />
            <Button variant="ghost" disabled={!pilot} onClick={() => setPilot("")}>
              Без пилота
            </Button>
          </div>
          <p className={`${fieldStyles.hint} ${styles.pilotHint}`} id={pilotHintId}>
            Последний день пилота: за неделю до него компания подсветится в списке. Пусто — без
            пилота.
          </p>
        </div>
        <Switch
          label="Компания работает"
          checked={active}
          onCheckedChange={setActive}
          disabled={own && company.is_active}
          hint={
            own && company.is_active
              ? "Это ваша текущая компания — из панели её не приостановить: переключитесь на другую."
              : "Выключенная компания — на паузе: сотрудники не могут войти, данные сохраняются."
          }
        />
        {pausing ? (
          <Notice kind="warn" title="Компания встанет на паузу">
            Сразу после сохранения все сотрудники компании потеряют доступ: не смогут задавать
            вопросы и открывать документы. Данные и настройки останутся — включить компанию можно
            здесь же.
          </Notice>
        ) : null}
      </form>
    </Modal>
  );
}
