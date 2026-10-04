import { useQuery } from "@tanstack/react-query";
import { Building2, Ticket } from "lucide-react";
import { useState, type SubmitEvent } from "react";
import { Link, useNavigate } from "react-router";

import { api, unwrap } from "../api/client";
import { useMe } from "../auth/context";
import { inviteSecretFrom, savePendingInvite } from "../auth/pendingInvite";
import { formatDate } from "../lib/format";
import { useDocumentTitle } from "../lib/title";
import { Button } from "../ui/Button";
import { buttonClass } from "../ui/buttonClass";
import { Card } from "../ui/Card";
import { TextField } from "../ui/Field";
import { Notice } from "../ui/Notice";
import { Page, PageHeader } from "../ui/Page";
import styles from "./NoCompanyHome.module.css";

/**
 * Главная учётки без компании (ТЗ §2): вступить по ссылке или коду
 * приглашения либо попросить подключить свою компанию — заявку одобряет
 * команда kronto.
 */
export function NoCompanyHome() {
  useDocumentTitle("Главная");
  const me = useMe();
  const waiting = me.companies.filter((company) => company.status === "pending");
  const requests = useQuery({
    queryKey: ["company-requests"],
    queryFn: () => unwrap(api.GET("/api/v1/account/company-requests")),
  });
  const openRequest = requests.data?.find((request) => request.status === "new");

  return (
    <Page>
      <PageHeader
        title={me.first_name ? `Здравствуйте, ${me.first_name}` : "Здравствуйте"}
        description="Вы ещё не состоите в компании. kronto отвечает по документам компании, поэтому начнём с неё."
      />
      {waiting.length ? (
        <div className={styles.notice}>
          <Notice kind="info" title="Ждём администратора">
            {waiting.length === 1
              ? `Заявка на вступление в «${waiting[0]?.company_name ?? ""}» ждёт одобрения.`
              : `Заявки ждут одобрения: ${waiting.map((c) => `«${c.company_name}»`).join(", ")}.`}
          </Notice>
        </div>
      ) : null}
      <div className={styles.grid}>
        <Card className={styles.card}>
          <Ticket size={28} aria-hidden className={styles.icon} />
          <h2 className={styles.title}>Вступить в компанию</h2>
          <p className="muted">
            Ссылку или код приглашения даёт администратор вашей компании — обычно в рабочем чате.
          </p>
          <JoinForm />
        </Card>
        <Card className={styles.card}>
          <Building2 size={28} aria-hidden className={styles.icon} />
          <h2 className={styles.title}>Подключить свою компанию</h2>
          {openRequest ? (
            <p>
              Заявка на «{openRequest.company_name}» отправлена {formatDate(openRequest.created_at)}
              . Мы свяжемся с вами, создадим компанию и сделаем вас администратором.
            </p>
          ) : (
            <p className="muted">
              Оставьте заявку — команда kronto свяжется с вами, создаст компанию и сделает вас
              администратором.
            </p>
          )}
          <Link to="/settings/companies" className={buttonClass("ghost", "md", false)}>
            {openRequest ? "Заявки и компании" : "Оставить заявку"}
          </Link>
        </Card>
      </div>
    </Page>
  );
}

function JoinForm() {
  const navigate = useNavigate();
  const [value, setValue] = useState("");

  function submit(event: SubmitEvent) {
    event.preventDefault();
    const secret = inviteSecretFrom(value);
    if (!secret) return;
    savePendingInvite(secret);
    void navigate("/join");
  }

  return (
    <form className={styles.form} onSubmit={submit}>
      <TextField
        label="Ссылка или код приглашения"
        name="invite"
        autoComplete="off"
        autoCapitalize="characters"
        spellCheck={false}
        required
        maxLength={512}
        placeholder="K7QM-4XPA"
        value={value}
        onChange={(e) => setValue(e.target.value)}
      />
      <Button type="submit">Продолжить</Button>
    </form>
  );
}
