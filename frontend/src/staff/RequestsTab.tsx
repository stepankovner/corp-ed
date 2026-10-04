import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Inbox } from "lucide-react";
import { useId, useState, type SubmitEvent } from "react";

import adminStyles from "../admin/Admin.module.css";
import { ConfirmDialog } from "../admin/common";
import companyStyles from "../admin/Company.module.css";
import { ChoiceCards, type Choice } from "../admin/CompanySettingsPage";
import { api, unwrap, type Schemas } from "../api/client";
import { ApiError, errorMessage } from "../api/errors";
import { formatDateTime, formatNumber, plural } from "../lib/format";
import { formatPrice, TARIFFS, type TariffCode } from "../lib/tariffs";
import { useDocumentTitle } from "../lib/title";
import { Badge, type Tone } from "../ui/Badge";
import { Button } from "../ui/Button";
import { TextField } from "../ui/Field";
import { Modal } from "../ui/Modal";
import { Notice } from "../ui/Notice";
import { EmptyState } from "../ui/Page";
import pageStyles from "../ui/Page.module.css";
import { SegmentedControl } from "../ui/SegmentedControl";
import { SkeletonList } from "../ui/Skeleton";
import { useToast } from "../ui/useToast";
import { STAFF_KEY } from "./keys";

type Request = Schemas["StaffRequestResponse"];
type Filter = "new" | "all";

/** Как на бэкенде: мест не больше (StaffApproveRequest), без числа в заявке — столько. */
const MAX_SEATS = 10_000;
const DEFAULT_SEATS = 10;

const FILTERS: { value: Filter; label: string }[] = [
  { value: "new", label: "Новые" },
  { value: "all", label: "Все" },
];

const STATUS: Record<Exclude<Request["status"], "new">, { label: string; tone: Tone }> = {
  approved: { label: "одобрена", tone: "ok" },
  rejected: { label: "отклонена", tone: "error" },
  cancelled: { label: "отозвана", tone: "muted" },
};

/** Тарифы карточками, как на странице «Тариф», — но без описаний: команда их знает. */
const TARIFF_CHOICES: Choice<TariffCode>[] = TARIFFS.map((tariff) => ({
  value: tariff.code,
  title: tariff.name,
  text: (
    <span className={companyStyles.choicePrice}>
      {tariff.price === null ? "Цена по запросу" : `${formatPrice(tariff.price)} за место в месяц`}
    </span>
  ),
}));

/** Сегодня по часам браузера — «ГГГГ-ММ-ДД», как у поля даты. */
function today(): string {
  const now = new Date();
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())}`;
}

/** Кто подал заявку — для текста окна: «Анна Смирнова (anna@…)». */
function applicant(request: Request): string {
  const { applicant_name: name, applicant_email: email } = request;
  if (name && email) return `${name} (${email})`;
  return name || email || "Заявитель";
}

/**
 * Заявки «Подключить компанию» (ТЗ §2, §9): одобрение создаёт компанию,
 * заявитель становится её администратором и получает письмо; отказ —
 * тоже письмом. Раньше это делали командой cli requests.
 */
export function RequestsTab() {
  useDocumentTitle("Заявки");
  const queryClient = useQueryClient();
  const toast = useToast();
  const [filter, setFilter] = useState<Filter>("new");
  const [approving, setApproving] = useState<Request | null>(null);
  const [rejecting, setRejecting] = useState<Request | null>(null);
  const requests = useQuery({
    queryKey: [...STAFF_KEY, "requests", filter],
    queryFn: () =>
      unwrap(api.GET("/api/v1/staff/requests", { params: { query: { status: filter } } })),
  });
  // Всю панель: число новых заявок в шапке — тоже.
  const refresh = () => queryClient.invalidateQueries({ queryKey: STAFF_KEY });

  return (
    <>
      <div className={adminStyles.toolbar}>
        <SegmentedControl<Filter>
          label="Какие заявки показать"
          value={filter}
          options={FILTERS}
          onChange={setFilter}
        />
      </div>
      {requests.isPending ? (
        <SkeletonList label="Загрузка заявок" />
      ) : requests.isError ? (
        <Notice kind="error">{errorMessage(requests.error)}</Notice>
      ) : requests.data.length === 0 ? (
        <EmptyState
          icon={<Inbox size={32} aria-hidden />}
          title={filter === "new" ? "Новых заявок нет" : "Заявок пока нет"}
        >
          <p>
            Заявку «Подключить компанию» оставляет человек с учёткой kronto без компании. Новая
            приходит команде в Telegram и появляется здесь.
          </p>
        </EmptyState>
      ) : (
        <ul className={adminStyles.cards} aria-label="Заявки на подключение компаний">
          {requests.data.map((request) => (
            <RequestCard
              key={request.id}
              request={request}
              onApprove={() => setApproving(request)}
              onReject={() => setRejecting(request)}
            />
          ))}
        </ul>
      )}
      {approving ? <ApproveDialog request={approving} onClose={() => setApproving(null)} /> : null}
      <ConfirmDialog
        open={rejecting !== null}
        onOpenChange={(open) => !open && setRejecting(null)}
        title="Отклонить заявку?"
        description={
          rejecting
            ? `Компанию «${rejecting.company_name}» не создадим.${rejecting.applicant_email ? ` На ${rejecting.applicant_email} уйдёт письмо: заявку пока не можем одобрить.` : ""}`
            : undefined
        }
        confirmLabel="Отклонить"
        onConfirm={async () => {
          if (!rejecting) return;
          const { id, company_name: name } = rejecting;
          try {
            await unwrap(
              api.POST("/api/v1/staff/requests/{request_id}/reject", {
                params: { path: { request_id: id } },
              }),
            );
          } catch (error) {
            // Заявку уже рассмотрели — список устарел.
            if (error instanceof ApiError && error.status === 409) void refresh();
            throw error;
          }
          toast.show(`Заявка «${name}» отклонена`);
          await refresh();
        }}
      />
    </>
  );
}

function RequestCard({
  request,
  onApprove,
  onReject,
}: {
  request: Request;
  onApprove: () => void;
  onReject: () => void;
}) {
  const status = request.status === "new" ? null : STATUS[request.status];
  const { applicant_name: name, applicant_email: email } = request;
  return (
    <li className={adminStyles.card}>
      <div className={adminStyles.cardHead}>
        {/* Длинные название и почта переносятся, а не раздвигают карточку на телефоне. */}
        <div style={{ minWidth: 0, overflowWrap: "anywhere" }}>
          <h2 className={adminStyles.cardTitle}>{request.company_name}</h2>
          <p className={companyStyles.small}>
            {name ? <>{name} · </> : null}
            {email ? (
              <a href={`mailto:${email}`}>{email}</a>
            ) : (
              <span className="muted">учётка заявителя удалена</span>
            )}
          </p>
        </div>
        {status ? (
          <Badge tone={status.tone}>{status.label}</Badge>
        ) : (
          <span className={pageStyles.row} style={{ gap: 8 }}>
            <Button
              variant="ghost"
              size="xs"
              aria-label={`Отклонить: ${request.company_name}`}
              onClick={onReject}
            >
              Отклонить
            </Button>
            {/* Без учётки заявителя компанию не создать: некому стать администратором. */}
            <Button
              size="xs"
              aria-label={`Одобрить: ${request.company_name}`}
              disabled={!email}
              onClick={onApprove}
            >
              Одобрить
            </Button>
          </span>
        )}
      </div>
      <div className={adminStyles.meta}>
        <span>
          {request.seats
            ? `${formatNumber(request.seats)} ${plural(request.seats, "место", "места", "мест")}`
            : "мест не указали"}
        </span>
        <span>заявка {formatDateTime(request.created_at)}</span>
        {request.decided_at ? (
          <span>
            {request.status === "cancelled" ? "отозвана" : "решение"}{" "}
            {formatDateTime(request.decided_at)}
          </span>
        ) : null}
      </div>
      {request.comment ? (
        <blockquote className={adminStyles.samples} style={{ overflowWrap: "anywhere" }}>
          {request.comment}
        </blockquote>
      ) : null}
    </li>
  );
}

/**
 * Одобрение: тариф, места и срок пилота. Окно монтируется на время
 * решения — поля не переживают закрытие.
 */
function ApproveDialog({ request, onClose }: { request: Request; onClose: () => void }) {
  const formId = useId();
  const queryClient = useQueryClient();
  const toast = useToast();
  const [tariff, setTariff] = useState<TariffCode>("base");
  const [seats, setSeats] = useState(String(request.seats ?? DEFAULT_SEATS));
  const [pilot, setPilot] = useState("");
  const [seatsError, setSeatsError] = useState<string | null>(null);
  const [pilotError, setPilotError] = useState<string | null>(null);
  const approve = useMutation({
    mutationFn: (body: Schemas["StaffApproveRequest"]) =>
      unwrap(
        api.POST("/api/v1/staff/requests/{request_id}/approve", {
          params: { path: { request_id: request.id } },
          body,
        }),
      ),
    onSuccess: async (company) => {
      await queryClient.invalidateQueries({ queryKey: STAFF_KEY });
      toast.show(`Компания «${company.name}» создана`);
      onClose();
    },
    onError: (error) => {
      // Заявку уже рассмотрели — список устарел.
      if (error instanceof ApiError && error.status === 409) {
        void queryClient.invalidateQueries({ queryKey: STAFF_KEY });
      }
    },
  });
  const minDate = today();

  function submit(event: SubmitEvent) {
    event.preventDefault();
    setSeatsError(null);
    setPilotError(null);
    const count = Number(seats);
    const seatsOk = seats.trim() !== "" && Number.isInteger(count) && count >= 1;
    if (!seatsOk || count > MAX_SEATS) {
      setSeatsError(`Укажите целое число от 1 до ${formatNumber(MAX_SEATS)}.`);
      return;
    }
    if (pilot && pilot < minDate) {
      setPilotError("Эта дата уже прошла.");
      return;
    }
    approve.mutate({ tariff, seats: count, ...(pilot ? { pilot_until: pilot } : {}) });
  }

  return (
    <Modal
      open
      onOpenChange={(open) => !open && !approve.isPending && onClose()}
      title="Одобрить заявку"
      description={`Создадим компанию «${request.company_name}». ${applicant(request)} станет её администратором и получит письмо со ссылкой на вход.`}
      footer={
        <>
          <Button variant="ghost" size="sm" onClick={onClose} disabled={approve.isPending}>
            Отмена
          </Button>
          <Button type="submit" form={formId} size="sm" busy={approve.isPending}>
            Создать компанию
          </Button>
        </>
      }
    >
      <form id={formId} className={pageStyles.form} onSubmit={submit} noValidate>
        {approve.isError ? <Notice kind="error">{errorMessage(approve.error)}</Notice> : null}
        <ChoiceCards label="Тариф" value={tariff} options={TARIFF_CHOICES} onChange={setTariff} />
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
          }}
          hint={
            request.seats
              ? `В заявке — ${formatNumber(request.seats)}.`
              : `В заявке не указали — по умолчанию ${DEFAULT_SEATS}.`
          }
          error={seatsError}
        />
        <TextField
          label="Пилот до"
          optional
          type="date"
          min={minDate}
          value={pilot}
          onChange={(e) => {
            setPilot(e.target.value);
            setPilotError(null);
          }}
          hint="Последний день пилота. Срок можно поставить и позже, на вкладке «Компании»."
          error={pilotError}
        />
      </form>
    </Modal>
  );
}
