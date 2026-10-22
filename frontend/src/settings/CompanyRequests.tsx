import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState, type SubmitEvent } from "react";
import { Link } from "react-router";

import { api, unwrap, type Schemas } from "../api/client";
import { ApiError, errorMessage } from "../api/errors";
import { useMe } from "../auth/context";
import { formatDate, plural } from "../lib/format";
import { Badge, type Tone } from "../ui/Badge";
import { Button } from "../ui/Button";
import { TextAreaField, TextField } from "../ui/Field";
import { Notice } from "../ui/Notice";
import { SkeletonList } from "../ui/Skeleton";
import { useToast } from "../ui/useToast";
import { Section } from "./common";
import { REQUESTS_KEY } from "./keys";
import styles from "./Settings.module.css";

type Request = Schemas["CompanyRequestResponse"];

const STATUS: Record<Request["status"], { label: string; tone: Tone }> = {
  new: { label: "на рассмотрении", tone: "warn" },
  approved: { label: "одобрена", tone: "ok" },
  rejected: { label: "отклонена", tone: "error" },
  cancelled: { label: "отменена", tone: "muted" },
};

// Ограничения — как в схеме бэкенда (CompanyRequestCreate).
const MAX_COMPANY_NAME = 200;
const MAX_SEATS = 10_000;
const MAX_COMMENT = 2000;
const INN_SHAPE = /^(\d{10}|\d{12})$/;

/**
 * «Подключить свою компанию» (ТЗ §2): заявку одобряет команда kronto,
 * заявитель становится администратором. Оплата в интерфейсе — после
 * первого пилота.
 */
export function CompanyRequests() {
  const me = useMe();
  const queryClient = useQueryClient();
  const toast = useToast();
  const requests = useQuery({
    queryKey: REQUESTS_KEY,
    queryFn: () => unwrap(api.GET("/api/v1/account/company-requests")),
  });
  const cancel = useMutation({
    mutationFn: (id: string) =>
      unwrap(
        api.POST("/api/v1/account/company-requests/{request_id}/cancel", {
          params: { path: { request_id: id } },
        }),
      ),
    onSuccess: async () => {
      toast.show("Заявка отменена");
      await queryClient.invalidateQueries({ queryKey: REQUESTS_KEY });
    },
  });
  const open = requests.data?.find((item) => item.status === "new");

  return (
    <Section
      title="Подключить свою компанию"
      description={
        <>
          Заявку рассматривает команда kronto: после одобрения компания появится в списке выше, а вы
          станете её администратором. <Link to="/pricing">Тарифы</Link>
        </>
      }
    >
      {cancel.isError ? <Notice kind="error">{errorMessage(cancel.error)}</Notice> : null}
      {requests.isPending ? (
        <SkeletonList rows={1} label="Загрузка заявок" />
      ) : requests.isError ? (
        <Notice kind="error">{errorMessage(requests.error)}</Notice>
      ) : (
        <>
          {requests.data.length ? (
            <ul className={styles.list} aria-label="Ваши заявки">
              {requests.data.map((item) => (
                <li key={item.id} className={styles.item}>
                  <div className={styles.itemMain}>
                    <div className={styles.itemText}>
                      <span className={styles.itemTitle}>
                        {item.company_name}
                        <Badge tone={STATUS[item.status].tone}>{STATUS[item.status].label}</Badge>
                      </span>
                      <span className={styles.meta}>
                        <span>Подана {formatDate(item.created_at)}</span>
                        {item.seats ? (
                          <span>
                            {item.seats} {plural(item.seats, "место", "места", "мест")}
                          </span>
                        ) : null}
                      </span>
                    </div>
                  </div>
                  {item.status === "new" ? (
                    <Button
                      variant="ghost"
                      size="xs"
                      busy={cancel.isPending && cancel.variables === item.id}
                      disabled={cancel.isPending}
                      onClick={() => cancel.mutate(item.id)}
                    >
                      Отменить заявку
                    </Button>
                  ) : null}
                </li>
              ))}
            </ul>
          ) : null}
          {open ? (
            <p className={`muted ${styles.small}`}>
              Мы напишем на {me.email}, когда рассмотрим заявку. Новую можно подать после ответа или
              отменив эту.
            </p>
          ) : (
            <RequestForm />
          )}
        </>
      )}
    </Section>
  );
}

function RequestForm() {
  const queryClient = useQueryClient();
  const toast = useToast();
  const [company, setCompany] = useState("");
  const [inn, setInn] = useState("");
  const [serverInnProblem, setServerInnProblem] = useState<string | null>(null);
  const [seats, setSeats] = useState("");
  const [comment, setComment] = useState("");
  const [touched, setTouched] = useState(false);
  const name = company.split(/\s+/).filter(Boolean).join(" ");
  const seatsValue = seats.trim() ? Number(seats) : null;
  const nameProblem = name.length < 2 ? "Укажите название компании." : null;
  const innValue = inn.trim();
  const innProblem = !INN_SHAPE.test(innValue)
    ? "ИНН — 10 цифр у организации или 12 у ИП."
    : serverInnProblem;
  const seatsProblem =
    seatsValue !== null &&
    (!Number.isInteger(seatsValue) || seatsValue < 1 || seatsValue > MAX_SEATS)
      ? "Целое число от 1 до 10 000."
      : null;

  const create = useMutation({
    mutationFn: () =>
      unwrap(
        api.POST("/api/v1/account/company-requests", {
          body: {
            company_name: name,
            inn: innValue,
            seats: seatsValue,
            comment: comment.trim() || null,
          },
        }),
      ),
    onSuccess: async () => {
      toast.show("Заявка отправлена");
      await queryClient.invalidateQueries({ queryKey: REQUESTS_KEY });
    },
    onError: async (err) => {
      // Контрольные цифры ИНН проверяет сервер — ошибку показываем у поля.
      if (err instanceof ApiError && err.field === "inn") {
        setServerInnProblem(err.message);
        return;
      }
      // Заявка уже есть (например, подана в другой вкладке) — покажем её
      // в списке; форма тогда пропадёт, поэтому ответ — и всплывающим.
      if (err instanceof ApiError && err.code === "company_request_exists") {
        toast.show(err.message, { tone: "info" });
        await queryClient.invalidateQueries({ queryKey: REQUESTS_KEY });
      }
    },
  });

  function submit(event: SubmitEvent) {
    event.preventDefault();
    setTouched(true);
    if (nameProblem || innProblem || seatsProblem) return;
    create.mutate();
  }

  const innError = create.error instanceof ApiError && create.error.field === "inn";
  return (
    <form className={styles.form} onSubmit={submit} noValidate>
      {create.isError && !innError ? (
        <Notice kind="error">{errorMessage(create.error)}</Notice>
      ) : null}
      <TextField
        label="Название компании"
        autoComplete="organization"
        required
        maxLength={MAX_COMPANY_NAME}
        value={company}
        onChange={(e) => setCompany(e.target.value)}
        error={touched ? nameProblem : null}
      />
      <TextField
        label="ИНН компании"
        inputMode="numeric"
        autoComplete="off"
        required
        maxLength={12}
        value={inn}
        onChange={(e) => {
          setInn(e.target.value);
          setServerInnProblem(null);
        }}
        hint="10 цифр у организации, 12 — у ИП."
        error={touched || serverInnProblem ? innProblem : null}
      />
      <TextField
        label="Сколько сотрудников будут пользоваться"
        optional
        type="number"
        inputMode="numeric"
        min={1}
        max={MAX_SEATS}
        value={seats}
        onChange={(e) => setSeats(e.target.value)}
        hint="Ориентир для тарифа и числа мест."
        error={touched ? seatsProblem : null}
      />
      <TextAreaField
        label="Комментарий"
        optional
        maxLength={MAX_COMMENT}
        rows={3}
        value={comment}
        onChange={(e) => setComment(e.target.value)}
        hint="Например, какие системы хотите подключить и когда удобно созвониться."
      />
      <div className={styles.actions}>
        <Button type="submit" size="sm" busy={create.isPending}>
          Отправить заявку
        </Button>
      </div>
    </form>
  );
}
