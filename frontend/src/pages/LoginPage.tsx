import { useState, type SubmitEvent } from "react";
import { Link } from "react-router";

import { errorMessage } from "../api/errors";
import { rememberedCompany, useAuth } from "../auth/context";
import { useDocumentTitle } from "../lib/title";
import { Button } from "../ui/Button";
import { TextField } from "../ui/Field";
import { Notice } from "../ui/Notice";
import authStyles from "./AuthLayout.module.css";
import { AuthLayout } from "./AuthLayout";

export function LoginPage() {
  useDocumentTitle("Вход");
  const { login } = useAuth();
  const [company, setCompany] = useState(rememberedCompany);
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit(event: SubmitEvent) {
    event.preventDefault();
    setError(null);
    setBusy(true);
    try {
      await login(company.trim(), email.trim(), password);
    } catch (err) {
      setError(errorMessage(err));
      setBusy(false);
    }
  }

  return (
    <AuthLayout
      bar="Kronto"
      title="Вход"
      lead="Ответы по документам вашей компании — со ссылкой на источник."
    >
      <form className={authStyles.form} onSubmit={submit} noValidate={false}>
        {error ? <Notice kind="error">{error}</Notice> : null}
        <TextField
          label="Код компании"
          name="company"
          autoComplete="organization"
          required
          maxLength={63}
          value={company}
          onChange={(e) => setCompany(e.target.value)}
          hint="Его выдаёт администратор вместе с доступом."
          autoFocus={!company}
        />
        <TextField
          label="Рабочая почта"
          name="email"
          type="email"
          autoComplete="username"
          required
          value={email}
          onChange={(e) => setEmail(e.target.value)}
          autoFocus={Boolean(company)}
        />
        <TextField
          label="Пароль"
          name="password"
          type="password"
          autoComplete="current-password"
          required
          maxLength={128}
          value={password}
          onChange={(e) => setPassword(e.target.value)}
        />
        <Button type="submit" block busy={busy}>
          Войти
        </Button>
        <p className="muted" style={{ fontSize: "var(--fs-small)" }}>
          Компания ещё не подключена? <Link to="/pricing">Тарифы и запись на созвон</Link>
        </p>
      </form>
    </AuthLayout>
  );
}
