import { useEffect, useRef, useState, type SubmitEvent } from "react";
import { Link, useLocation, useNavigate, useSearchParams } from "react-router";

import { api, unwrap } from "../api/client";
import { errorMessage } from "../api/errors";
import { useAuth, type Tokens } from "../auth/context";
import { useHashSecret } from "../auth/hashSecret";
import { safeNext } from "../auth/next";
import { pendingInvite } from "../auth/pendingInvite";
import { useDocumentTitle } from "../lib/title";
import { Button } from "../ui/Button";
import { TextField } from "../ui/Field";
import { Notice } from "../ui/Notice";
import { PageSpinner } from "../ui/Spinner";
import authStyles from "./AuthLayout.module.css";
import { AuthLayout } from "./AuthLayout";
import { ResendLink } from "./ResendLink";

interface VerifyState {
  email?: string;
  /** Пришли со входа: пароль верный, а почта не подтверждена. */
  fromLogin?: boolean;
}

/**
 * Подтверждение почты (ТЗ §3): код из письма на этой странице или ссылка
 * /verify-email#token=… — письмо могли открыть на другом устройстве.
 * Подтвердили — сразу вход; было приглашение — к нему.
 */
export function VerifyEmailPage() {
  useDocumentTitle("Подтверждение почты");
  const token = useHashSecret("token");
  const { signIn } = useAuth();
  const navigate = useNavigate();
  const [params] = useSearchParams();

  async function done(tokens: Tokens) {
    await signIn(tokens);
    void navigate(pendingInvite() ? "/join" : safeNext(params.get("next")), { replace: true });
  }

  if (token) return <ByLink token={token} onVerified={done} />;
  return <ByCode onVerified={done} />;
}

function ByLink({
  token,
  onVerified,
}: {
  token: string;
  onVerified: (tokens: Tokens) => Promise<void>;
}) {
  const [error, setError] = useState<string | null>(null);
  const started = useRef(false);

  useEffect(() => {
    // Ссылка одноразовая: второй запрос (StrictMode) получил бы отказ.
    if (started.current) return;
    started.current = true;
    unwrap(api.POST("/api/v1/auth/verify-email/link", { body: { token } }))
      .then(onVerified)
      .catch((err: unknown) => setError(errorMessage(err)));
  }, [token, onVerified]);

  if (!error) return <PageSpinner />;
  return (
    <AuthLayout bar="kronto" title="Ссылка не сработала">
      <div className={authStyles.form}>
        <Notice kind="error">{error}</Notice>
        <p className="muted">Войдите с почтой и паролем — мы предложим отправить новый код.</p>
        <Link to="/login">Ко входу</Link>
      </div>
    </AuthLayout>
  );
}

function ByCode({ onVerified }: { onVerified: (tokens: Tokens) => Promise<void> }) {
  const state = (useLocation().state ?? {}) as VerifyState;
  const [email, setEmail] = useState(state.email ?? "");
  const [code, setCode] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const knownEmail = Boolean(state.email);

  async function verify(value: string) {
    setError(null);
    setBusy(true);
    try {
      const tokens = await unwrap(
        api.POST("/api/v1/auth/verify-email", { body: { email: email.trim(), code: value } }),
      );
      await onVerified(tokens);
    } catch (err) {
      setCode("");
      setError(errorMessage(err));
      setBusy(false);
    }
  }

  function change(value: string) {
    const next = value.replace(/\D/g, "").slice(0, 6);
    setCode(next);
    if (next.length === 6 && email.trim() && !busy) void verify(next);
  }

  function submit(event: SubmitEvent) {
    event.preventDefault();
    void verify(code);
  }

  async function resend() {
    try {
      await unwrap(api.POST("/api/v1/auth/verify-email/resend", { body: { email: email.trim() } }));
    } catch (err) {
      setError(errorMessage(err));
      throw err;
    }
  }

  return (
    <AuthLayout
      bar="kronto"
      title="Подтвердите почту"
      lead={
        knownEmail
          ? `Отправили 6 цифр на ${email}. Код и ссылка в письме действуют 30 минут.`
          : "Введите почту и 6 цифр из письма."
      }
    >
      <form className={authStyles.form} onSubmit={submit}>
        {state.fromLogin ? (
          <Notice kind="info">
            Почта ещё не подтверждена. Код пришёл при регистрации; если письма нет — отправьте
            новое.
          </Notice>
        ) : null}
        {error ? <Notice kind="error">{error}</Notice> : null}
        {knownEmail ? null : (
          <TextField
            label="Почта"
            name="email"
            type="email"
            autoComplete="email"
            required
            maxLength={320}
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            autoFocus
          />
        )}
        <TextField
          label="Код из 6 цифр"
          name="code"
          className={authStyles.code}
          inputMode="numeric"
          autoComplete="one-time-code"
          required
          maxLength={6}
          value={code}
          onChange={(e) => change(e.target.value)}
          autoFocus={knownEmail}
          hint="Письмо не пришло — проверьте «Спам»."
        />
        <Button type="submit" block busy={busy}>
          Подтвердить
        </Button>
        {email.trim() ? <ResendLink onResend={resend} /> : null}
      </form>
      <p className={`muted ${authStyles.alt}`}>
        <Link to="/login">Ко входу</Link>
      </p>
    </AuthLayout>
  );
}
