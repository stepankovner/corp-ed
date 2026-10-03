import { useState, type SubmitEvent } from "react";
import { useNavigate } from "react-router";

import { ConfirmDialog } from "../admin/common";
import { api, unwrap } from "../api/client";
import { errorMessage } from "../api/errors";
import { useAuth, useMe, type Me } from "../auth/context";
import { inviteSecretFrom, savePendingInvite } from "../auth/pendingInvite";
import { companyInitials } from "../lib/initials";
import { useDocumentTitle } from "../lib/title";
import { Avatar } from "../ui/Avatar";
import { Badge } from "../ui/Badge";
import { Button } from "../ui/Button";
import { TextField } from "../ui/Field";
import { useToast } from "../ui/useToast";
import { Section } from "./common";
import { CompanyRequests } from "./CompanyRequests";
import styles from "./Settings.module.css";

type Membership = Me["companies"][number];

const ROLE_LABEL = { admin: "администратор", employee: "сотрудник" } as const;

/**
 * Компании учётки (ТЗ §2): переход, уход, вступление по приглашению и
 * заявка на подключение своей. Ушедшие членства не показываем — вернуться
 * можно только по новому приглашению.
 */
export function CompaniesTab() {
  useDocumentTitle("Компании");
  const me = useMe();
  const companies = me.companies.filter((item) => item.status !== "left");
  return (
    <div className={styles.stack}>
      <Section
        title="Ваши компании"
        description="В каждой компании — свои документы, вопросы и роль. Переключаться между ними можно и в боковой панели."
      >
        {companies.length ? (
          <ul className={styles.list} aria-label="Ваши компании">
            {companies.map((item) => (
              <CompanyRow
                key={item.tenant_id}
                item={item}
                current={item.tenant_id === me.company?.tenant_id}
              />
            ))}
          </ul>
        ) : (
          <p className={`muted ${styles.small}`}>
            Вы пока не состоите ни в одной компании. Вступите по приглашению от администратора или
            подключите свою компанию.
          </p>
        )}
      </Section>
      <JoinSection />
      <CompanyRequests />
    </div>
  );
}

function CompanyRow({ item, current }: { item: Membership; current: boolean }) {
  const { switchCompany, reloadMe } = useAuth();
  const navigate = useNavigate();
  const toast = useToast();
  const [switching, setSwitching] = useState(false);
  const [leaving, setLeaving] = useState(false);

  async function open() {
    setSwitching(true);
    try {
      await switchCompany(item.tenant_id);
      void navigate("/");
    } catch (err) {
      toast.show(errorMessage(err), { tone: "error" });
      setSwitching(false);
    }
  }

  return (
    <li className={styles.item}>
      <div className={styles.itemMain}>
        <Avatar
          shape="square"
          colorful
          name={item.company_name}
          initials={companyInitials(item.company_name)}
        />
        <div className={styles.itemText}>
          <span className={styles.itemTitle}>
            {item.company_name}
            {current ? <Badge tone="accent">вы здесь</Badge> : null}
          </span>
          <span className={styles.meta}>
            <span>{ROLE_LABEL[item.role]}</span>
            {item.status === "pending" ? <Badge tone="warn">ждёт одобрения</Badge> : null}
            {item.status === "blocked" ? <Badge tone="error">доступ закрыт</Badge> : null}
          </span>
        </div>
      </div>
      <div className={styles.itemActions}>
        {item.status === "active" && !current ? (
          <Button size="xs" busy={switching} onClick={() => void open()}>
            Перейти
          </Button>
        ) : null}
        <Button variant="ghost" size="xs" onClick={() => setLeaving(true)}>
          Выйти из компании
        </Button>
      </div>
      <ConfirmDialog
        open={leaving}
        onOpenChange={setLeaving}
        title={`Выйти из «${item.company_name}»?`}
        description={
          item.status === "pending"
            ? "Заявка на вступление отменится. Вернуться можно по новому приглашению."
            : "Доступ к документам и вопросам компании пропадёт, ваши диалоги в ней скроются и удалятся через 30 дней. Учётная запись останется; вернуться можно по новому приглашению."
        }
        confirmLabel="Выйти"
        onConfirm={async () => {
          await unwrap(api.POST("/api/v1/account/leave", { body: { tenant_id: item.tenant_id } }));
          // Ушли из текущей компании — токен с ней больше не действует,
          // профиль придёт уже без неё (refresh выдаст сессию без компании).
          await reloadMe();
          toast.show(`Вы вышли из «${item.company_name}»`);
        }}
      />
    </li>
  );
}

function JoinSection() {
  const navigate = useNavigate();
  const [value, setValue] = useState("");
  const [error, setError] = useState<string | null>(null);

  function submit(event: SubmitEvent) {
    event.preventDefault();
    const secret = inviteSecretFrom(value);
    if (!secret) {
      setError("Вставьте ссылку-приглашение или введите код.");
      return;
    }
    // Секрет — не в адресе: /join заберёт его из хранилища вкладки.
    savePendingInvite(secret);
    void navigate("/join");
  }

  return (
    <Section
      title="Вступить по приглашению"
      description="Ссылку или код приглашения даёт администратор компании."
    >
      <form className={styles.form} onSubmit={submit} noValidate>
        <TextField
          label="Ссылка или код приглашения"
          placeholder="K7QM-4XPA"
          autoComplete="off"
          autoCapitalize="off"
          spellCheck={false}
          value={value}
          onChange={(e) => {
            setValue(e.target.value);
            setError(null);
          }}
          error={error}
        />
        <div className={styles.actions}>
          <Button type="submit" size="sm">
            Продолжить
          </Button>
        </div>
      </form>
    </Section>
  );
}
