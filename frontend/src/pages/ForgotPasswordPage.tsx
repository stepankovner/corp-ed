import { useState, type SubmitEvent } from "react";
import { Link } from "react-router";

import { api, unwrap } from "../api/client";
import { errorMessage } from "../api/errors";
import { useDocumentTitle } from "../lib/title";
import { Button } from "../ui/Button";
import { TextField } from "../ui/Field";
import { Notice } from "../ui/Notice";
import authStyles from "./AuthLayout.module.css";
import { AuthLayout } from "./AuthLayout";
import { ResendLink } from "./ResendLink";

/**
 * Восстановление пароля (ТЗ §3): ссылка на почту. Ответ сервера один и
 * тот же, есть учётка или нет, — по форме не перебрать адреса.
 */
export function ForgotPasswordPage() {
  useDocumentTitle("Восстановление пароля");
  const [email, setEmail] = useState("");
  const [sentTo, setSentTo] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function send(address: string) {
    const sent = await unwrap(
      api.POST("/api/v1/auth/forgot-password", { body: { email: address } }),
    );
    setSentTo(sent.email);
  }

  async function submit(event: SubmitEvent) {
    event.preventDefault();
    setError(null);
    setBusy(true);
    try {
      await send(email.trim());
    } catch (err) {
      setError(errorMessage(err));
    }
    setBusy(false);
  }

  if (sentTo) {
    return (
      <AuthLayout bar="kronto" title="Проверьте почту">
        <div className={authStyles.form}>
          {error ? <Notice kind="error">{error}</Notice> : null}
          <p>
            Если учётная запись с адресом <strong>{sentTo}</strong> есть, мы отправили на него
            ссылку для нового пароля. Она действует час и срабатывает один раз.
          </p>
          <p className="muted">Письмо не пришло — проверьте «Спам».</p>
          <ResendLink
            onResend={() =>
              send(sentTo).catch((err: unknown) => {
                setError(errorMessage(err));
                throw err;
              })
            }
          />
          <Link to="/login">Ко входу</Link>
        </div>
      </AuthLayout>
    );
  }

  return (
    <AuthLayout
      bar="kronto"
      title="Восстановление пароля"
      lead="Пришлём ссылку, по которой можно задать новый пароль."
    >
      <form className={authStyles.form} onSubmit={submit}>
        {error ? <Notice kind="error">{error}</Notice> : null}
        <TextField
          label="Почта"
          name="email"
          type="email"
          autoComplete="username"
          required
          maxLength={320}
          value={email}
          onChange={(e) => setEmail(e.target.value)}
          autoFocus
        />
        <Button type="submit" block busy={busy}>
          Отправить ссылку
        </Button>
      </form>
      <p className={`muted ${authStyles.alt}`}>
        Вспомнили пароль? <Link to="/login">Войти</Link>
      </p>
    </AuthLayout>
  );
}
