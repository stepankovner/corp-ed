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

/**
 * Смена почты (ТЗ §3): пароль и второй фактор. С приложением или ключом —
 * код приложения или резервный в той же форме; без них сервер сначала
 * присылает код на прежний адрес, и форма просит его вторым шагом.
 */
function EmailSection() {
  const me = useMe();
  const strong = me.mfa.strong;
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [code, setCode] = useState("");
  // Код ушёл на прежний адрес: подсказка, куда именно.
  const [codeHint, setCodeHint] = useState<string | null>(null);
  const [needsCode, setNeedsCode] = useState(strong);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [taken, setTaken] = useState<string | null>(null);
  const [sent, setSent] = useState<string | null>(null);
  const awaitingMailCode = codeHint !== null;

  function reset() {
    setPassword("");
    setCode("");
    setCodeHint(null);
  }

  async function submit(event: SubmitEvent) {
    event.preventDefault();
    const next = email.trim();
    setError(null);
    setTaken(null);
    setSent(null);
    setBusy(true);
    try {
      const step = await unwrap(
        api.POST("/api/v1/account/email", {
          body: { new_email: next, password, code: code.trim() || null },
        }),
      );
      if (step.status === "code_sent") {
        setCodeHint(step.email_hint ?? me.email);
        setCode("");
      } else {
        setSent(next);
        setEmail("");
        reset();
      }
    } catch (err) {
      setCode("");
      if (err instanceof ApiError && err.code === "email_taken") {
        setTaken(err.message);
        reset();
      } else if (err instanceof ApiError && err.code === "second_factor_required") {
        // Профиль устарел: приложение включили в другой вкладке.
        setNeedsCode(true);
      } else {
        if (err instanceof ApiError && err.code === "invalid_password") setPassword("");
        setError(errorMessage(err));
      }
    } finally {
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
          <Notice kind="ok" title="Проверьте новую почту">
            Мы отправили ссылку на {sent}; почта сменится после перехода по ней. Ссылка действует 24
            часа.
          </Notice>
        ) : null}
        {awaitingMailCode ? (
          <Notice kind="info" title="Подтвердите, что это вы">
            Отправили код на {codeHint} — текущий адрес учётной записи. Код действует 10 минут.
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
          readOnly={awaitingMailCode}
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
          readOnly={awaitingMailCode}
          value={password}
          onChange={(e) => setPassword(e.target.value)}
        />
        {needsCode || awaitingMailCode ? (
          <TextField
            label={awaitingMailCode ? "Код из письма" : "Код из приложения или резервный код"}
            name="code"
            inputMode={awaitingMailCode ? "numeric" : "text"}
            autoComplete="one-time-code"
            autoCapitalize="characters"
            spellCheck={false}
            required
            maxLength={12}
            value={code}
            onChange={(e) => setCode(e.target.value)}
            autoFocus={awaitingMailCode}
          />
        ) : null}
        <div className={styles.actions}>
          <Button
            type="submit"
            size="sm"
            busy={busy}
            disabled={
              !email.trim() || !password || ((needsCode || awaitingMailCode) && !code.trim())
            }
          >
            {awaitingMailCode ? "Подтвердить" : needsCode ? "Сменить почту" : "Получить код"}
          </Button>
          {awaitingMailCode ? (
            <Button type="button" size="sm" variant="ghost" onClick={reset}>
              Изменить адрес
            </Button>
          ) : null}
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
