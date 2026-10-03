import { useState, type ReactNode, type SubmitEvent } from "react";
import { Link, useNavigate, useSearchParams } from "react-router";

import { ApiError, errorMessage } from "../api/errors";
import { useAuth, type MfaChallenge } from "../auth/context";
import { useDocumentTitle } from "../lib/title";
import { Button } from "../ui/Button";
import { Checkbox, TextField } from "../ui/Field";
import { Notice } from "../ui/Notice";
import authStyles from "./AuthLayout.module.css";
import { AuthLayout } from "./AuthLayout";
import { SecondFactorStep } from "./SecondFactorStep";

/**
 * Вход в два шага (ТЗ §3): почта и пароль, затем второй фактор — код из
 * письма, приложения, резервный код или ключ доступа. Доверенное
 * устройство («запомнить на 30 дней») второй шаг пропускает.
 */
export function LoginPage() {
  useDocumentTitle("Вход");
  const [params] = useSearchParams();
  const [challenge, setChallenge] = useState<MfaChallenge | null>(null);
  const [expired, setExpired] = useState(false);
  const query = params.toString();
  const suffix = query ? `?${query}` : "";

  if (challenge) {
    return (
      <SecondFactorStep
        challenge={challenge}
        onRestart={() => {
          setChallenge(null);
          setExpired(true);
        }}
        onBack={() => setChallenge(null)}
      />
    );
  }
  return (
    <AuthLayout
      bar="kronto"
      title="Вход"
      lead="Ответы по документам вашей компании — со ссылкой на источник."
    >
      <PasswordStep
        onChallenge={(next) => {
          setExpired(false);
          setChallenge(next);
        }}
        notice={
          expired ? (
            <Notice kind="warn">Время на подтверждение вышло — войдите ещё раз.</Notice>
          ) : null
        }
      />
      <p className={`muted ${authStyles.alt}`}>
        Нет учётной записи? <Link to={`/register${suffix}`}>Зарегистрироваться</Link>
      </p>
      <p className={`muted ${authStyles.alt}`}>
        Компания ещё не подключена? <Link to="/pricing">Тарифы и запись на созвон</Link>
      </p>
    </AuthLayout>
  );
}

function PasswordStep({
  onChallenge,
  notice,
}: {
  onChallenge: (challenge: MfaChallenge) => void;
  notice: ReactNode;
}) {
  const { login } = useAuth();
  const navigate = useNavigate();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [remember, setRemember] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit(event: SubmitEvent) {
    event.preventDefault();
    setError(null);
    setBusy(true);
    try {
      const outcome = await login(email.trim(), password, remember);
      // Вошли сразу (доверенное устройство) — PublicOnly уведёт дальше.
      if (outcome.status === "mfa") onChallenge(outcome.challenge);
      return;
    } catch (err) {
      if (err instanceof ApiError && err.code === "email_not_verified") {
        void navigate("/verify-email", { state: { email: email.trim(), fromLogin: true } });
        return;
      }
      setError(errorMessage(err));
    }
    setBusy(false);
  }

  return (
    <form className={authStyles.form} onSubmit={submit}>
      {notice}
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
      <TextField
        label="Пароль"
        name="password"
        type="password"
        autoComplete="current-password"
        required
        maxLength={128}
        value={password}
        onChange={(e) => setPassword(e.target.value)}
        hint={<Link to="/forgot-password">Забыли пароль?</Link>}
      />
      <Checkbox
        label="Запомнить это устройство на 30 дней"
        checked={remember}
        onChange={(e) => setRemember(e.target.checked)}
      />
      <Button type="submit" block busy={busy}>
        Войти
      </Button>
    </form>
  );
}
