import { useState, type SubmitEvent } from "react";
import { useNavigate } from "react-router";

import { api, unwrap } from "../api/client";
import { ApiError, errorMessage } from "../api/errors";
import { useAuth, useMe } from "../auth/context";
import { useDocumentTitle } from "../lib/title";
import { Button } from "../ui/Button";
import { TextField } from "../ui/Field";
import { Notice } from "../ui/Notice";
import { useToast } from "../ui/useToast";
import { PasswordDialog, Section } from "./common";
import styles from "./Settings.module.css";

/** Смена почты и удаление учётки (ТЗ §2–3) — не на виду, отдельной вкладкой. */
export function AccountTab() {
  useDocumentTitle("Управление учётной записью");
  return (
    <div className={styles.stack}>
      <EmailSection />
      <DeleteSection />
    </div>
  );
}

function EmailSection() {
  const me = useMe();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [taken, setTaken] = useState<string | null>(null);
  const [sent, setSent] = useState<string | null>(null);

  async function submit(event: SubmitEvent) {
    event.preventDefault();
    const next = email.trim();
    setError(null);
    setTaken(null);
    setSent(null);
    setBusy(true);
    try {
      await unwrap(api.POST("/api/v1/account/email", { body: { new_email: next, password } }));
      setSent(next);
      setEmail("");
    } catch (err) {
      if (err instanceof ApiError && err.code === "email_taken") setTaken(err.message);
      else setError(errorMessage(err));
    } finally {
      setPassword("");
      setBusy(false);
    }
  }

  return (
    <Section
      title="Почта"
      description={
        <>
          Сейчас: <strong>{me.email}</strong>. После смены на прежний адрес придёт письмо: если это
          были не вы, по ссылке из него смену можно отменить в течение 7 дней.
        </>
      }
    >
      <div aria-live="polite">
        {sent ? (
          <Notice kind="ok" title="Проверьте почту">
            Мы отправили ссылку на {sent}; почта сменится после перехода по ней. Ссылка действует 24
            часа.
          </Notice>
        ) : null}
      </div>
      <form className={styles.form} onSubmit={submit}>
        {error ? <Notice kind="error">{error}</Notice> : null}
        <input
          type="email"
          name="username"
          autoComplete="username"
          value={me.email}
          readOnly
          hidden
        />
        <TextField
          label="Новая почта"
          type="email"
          autoComplete="email"
          required
          value={email}
          onChange={(e) => {
            setEmail(e.target.value);
            setTaken(null);
          }}
          error={taken}
        />
        <TextField
          label="Пароль от учётной записи"
          type="password"
          autoComplete="current-password"
          required
          value={password}
          onChange={(e) => setPassword(e.target.value)}
        />
        <div className={styles.actions}>
          <Button type="submit" size="sm" busy={busy} disabled={!email.trim() || !password}>
            Сменить почту
          </Button>
        </div>
      </form>
    </Section>
  );
}

function DeleteSection() {
  const { logout } = useAuth();
  const navigate = useNavigate();
  const toast = useToast();
  const [confirming, setConfirming] = useState(false);

  return (
    <Section
      danger
      title="Удаление учётной записи"
      description="Отменить удаление нельзя. Если вы единственный администратор компании, сначала назначьте другого."
    >
      <ul className={styles.bullets}>
        <li>Вы выйдете из всех компаний; ваши диалоги в них скроются и удалятся через 30 дней.</li>
        <li>Имя, почта, пароль, ключи доступа и заявки на подключение компании удалятся сразу.</li>
        <li>Вернуться можно только с новой регистрацией и по новым приглашениям.</li>
      </ul>
      <div className={styles.actions}>
        <Button variant="danger" size="sm" onClick={() => setConfirming(true)}>
          Удалить учётную запись
        </Button>
      </div>
      {confirming ? (
        <PasswordDialog
          title="Удалить учётную запись?"
          description="Это действие необратимо. Введите пароль, чтобы подтвердить, что это вы."
          confirmLabel="Удалить навсегда"
          danger
          onClose={() => setConfirming(false)}
          onConfirm={async (password) => {
            await unwrap(api.POST("/api/v1/account/delete", { body: { password } }));
            // Сессии на сервере больше нет — здесь забываем токен и данные.
            await logout();
            toast.show("Учётная запись удалена", { tone: "info" });
            void navigate("/login", { replace: true });
          }}
        />
      ) : null}
    </Section>
  );
}
