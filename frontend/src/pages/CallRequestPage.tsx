import { useMutation, useQuery } from "@tanstack/react-query";
import { useState, type SubmitEvent } from "react";
import { Link, useSearchParams } from "react-router";

import { api, unwrap, type Schemas } from "../api/client";
import { errorMessage } from "../api/errors";
import { formatDate } from "../lib/format";
import { priceLabel, tariffByCode, TARIFFS, type TariffCode } from "../lib/tariffs";
import { Button } from "../ui/Button";
import { Checkbox, SelectField, TextAreaField, TextField } from "../ui/Field";
import { Notice } from "../ui/Notice";
import { PageSpinner } from "../ui/Spinner";
import authStyles from "./AuthLayout.module.css";
import { AuthLayout } from "./AuthLayout";
import styles from "./PricingPage.module.css";

/**
 * Запись на созвон (досье 10.1): удобные дата и окно, данные компании и
 * телефон. Команда перезванивает и подтверждает время. Форма открыта,
 * только когда на сервере заданы политика обработки данных и её версия.
 */
export function CallRequestPage() {
  const form = useQuery({
    queryKey: ["lead-form"],
    queryFn: () => unwrap(api.GET("/api/v1/leads/form")),
    staleTime: 60_000,
  });

  if (form.isPending) return <PageSpinner />;
  return (
    <AuthLayout
      bar={<Link to="/pricing">Тарифы</Link>}
      title="Запись на созвон"
      lead="Покажем ассистента на ваших задачах и подключим компанию. Мы перезвоним, чтобы подтвердить время."
    >
      {form.isError ? (
        <Notice kind="error">{errorMessage(form.error)}</Notice>
      ) : !form.data.enabled ? (
        <Notice kind="info" title="Запись скоро откроется">
          Мы готовим форму записи. Загляните чуть позже.
        </Notice>
      ) : (
        <RequestForm form={form.data} />
      )}
    </AuthLayout>
  );
}

function RequestForm({ form }: { form: Schemas["LeadFormResponse"] }) {
  const [params] = useSearchParams();
  const [tariff, setTariff] = useState<TariffCode>(
    tariffByCode(params.get("tariff"))?.code ?? "base",
  );
  const [company, setCompany] = useState("");
  const [contact, setContact] = useState("");
  const [phone, setPhone] = useState("");
  const [email, setEmail] = useState("");
  const [seats, setSeats] = useState("");
  const [day, setDay] = useState("");
  const [slot, setSlot] = useState(form.slots[0] ?? "");
  const [comment, setComment] = useState("");
  const [consent, setConsent] = useState(false);
  const [trap, setTrap] = useState("");
  const send = useMutation({
    mutationFn: () =>
      unwrap(
        api.POST("/api/v1/leads", {
          body: {
            company_name: company.trim(),
            contact_name: contact.trim(),
            phone: phone.trim(),
            email: email.trim() || null,
            seats: Number(seats),
            tariff,
            preferred_date: day,
            preferred_slot: slot,
            comment: comment.trim() || null,
            policy_version: form.policy_version ?? "",
            consent,
            website: trap,
          },
        }),
      ),
  });

  if (send.isSuccess) {
    return (
      <Notice kind="ok" title="Заявка отправлена">
        Мы перезвоним по номеру {phone.trim()}, чтобы подтвердить созвон {formatDate(day)} в {slot}{" "}
        по Москве.
      </Notice>
    );
  }

  function submit(event: SubmitEvent) {
    event.preventDefault();
    send.mutate();
  }

  return (
    <form className={authStyles.form} onSubmit={submit}>
      {send.isError ? <Notice kind="error">{errorMessage(send.error)}</Notice> : null}
      <SelectField
        label="Тариф"
        value={tariff}
        onChange={(e) => setTariff(e.target.value as TariffCode)}
      >
        {TARIFFS.map((option) => (
          <option key={option.code} value={option.code}>
            {option.name} — {priceLabel(option)}
          </option>
        ))}
      </SelectField>
      <TextField
        label="Компания"
        required
        minLength={2}
        maxLength={200}
        autoComplete="organization"
        value={company}
        onChange={(e) => setCompany(e.target.value)}
      />
      <TextField
        label="Сколько сотрудников работают за компьютером"
        required
        type="number"
        inputMode="numeric"
        min={1}
        max={100000}
        value={seats}
        onChange={(e) => setSeats(e.target.value)}
      />
      <TextField
        label="Как к вам обращаться"
        required
        minLength={2}
        maxLength={200}
        autoComplete="name"
        value={contact}
        onChange={(e) => setContact(e.target.value)}
      />
      <TextField
        label="Телефон"
        required
        type="tel"
        autoComplete="tel"
        maxLength={24}
        placeholder="+7 999 123-45-67"
        value={phone}
        onChange={(e) => setPhone(e.target.value)}
      />
      <TextField
        label="Почта"
        optional
        type="email"
        autoComplete="email"
        value={email}
        onChange={(e) => setEmail(e.target.value)}
      />
      <TextField
        label="Удобная дата"
        required
        type="date"
        min={form.first_date}
        max={form.last_date}
        value={day}
        onChange={(e) => setDay(e.target.value)}
        hint="По будним дням."
      />
      <SelectField
        label="Удобное время (по Москве)"
        value={slot}
        onChange={(e) => setSlot(e.target.value)}
      >
        {form.slots.map((value) => (
          <option key={value} value={value}>
            {value}
          </option>
        ))}
      </SelectField>
      <TextAreaField
        label="Где сейчас лежат документы"
        optional
        maxLength={1000}
        rows={3}
        value={comment}
        onChange={(e) => setComment(e.target.value)}
        hint="Например: Битрикс24, сетевая папка, Confluence."
      />
      <div className={styles.trap} aria-hidden="true">
        <label>
          Сайт
          <input
            name="website"
            tabIndex={-1}
            autoComplete="off"
            value={trap}
            onChange={(e) => setTrap(e.target.value)}
          />
        </label>
      </div>
      <Checkbox
        required
        checked={consent}
        onChange={(e) => setConsent(e.target.checked)}
        label={
          <>
            Согласен на обработку персональных данных по{" "}
            <a href={form.policy_url ?? "#"} target="_blank" rel="noopener noreferrer">
              политике обработки данных
            </a>
          </>
        }
      />
      <Button type="submit" block busy={send.isPending} disabled={!consent}>
        Отправить заявку
      </Button>
    </form>
  );
}
