import { useState, type SubmitEvent } from "react";
import { Link, useNavigate, useSearchParams } from "react-router";

import { api, unwrap } from "../api/client";
import { ApiError, errorMessage } from "../api/errors";
import { passwordProblem } from "../auth/password";
import { pendingInvite } from "../auth/pendingInvite";
import { useDocumentTitle } from "../lib/title";
import { Button } from "../ui/Button";
import { Checkbox, TextField } from "../ui/Field";
import { Notice } from "../ui/Notice";
import authStyles from "./AuthLayout.module.css";
import { AuthLayout } from "./AuthLayout";

/**
 * Регистрация (ТЗ §2): учётка без компании, затем код на почту. Компания
 * — по приглашению или заявкой «Подключить компанию» уже после входа.
 * Пока на боевом домене нет юридических текстов, сервер пускает только с
 * приглашением (registration_closed).
 */
export function RegisterPage() {
  useDocumentTitle("Регистрация");
  const navigate = useNavigate();
  const [params] = useSearchParams();
  const invite = pendingInvite();
  const [firstName, setFirstName] = useState("");
  const [lastName, setLastName] = useState("");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [repeat, setRepeat] = useState("");
  const [consent, setConsent] = useState(false);
  const [touched, setTouched] = useState(false);
  const [error, setError] = useState<ApiError | string | null>(null);
  const [busy, setBusy] = useState(false);
  const problem = passwordProblem(password, repeat, email);
  const query = params.toString();

  async function submit(event: SubmitEvent) {
    event.preventDefault();
    setTouched(true);
    if (problem || !consent) return;
    setError(null);
    setBusy(true);
    try {
      const sent = await unwrap(
        api.POST("/api/v1/auth/register", {
          body: {
            first_name: firstName.trim(),
            last_name: lastName.trim(),
            email: email.trim(),
            password,
            consent: true,
            invite,
          },
        }),
      );
      void navigate(`/verify-email${query ? `?${query}` : ""}`, {
        state: { email: sent.email },
      });
    } catch (err) {
      setError(err instanceof ApiError ? err : errorMessage(err));
      setBusy(false);
    }
  }

  return (
    <AuthLayout
      bar="kronto"
      title="Регистрация"
      lead={
        invite
          ? "После подтверждения почты вернём вас к приглашению — останется нажать «Вступить»."
          : "Учётная запись — ваша: с ней можно состоять в нескольких компаниях."
      }
    >
      <form className={authStyles.form} onSubmit={submit}>
        {error ? <RegisterError error={error} /> : null}
        <div className={authStyles.pair}>
          <TextField
            label="Имя"
            name="first_name"
            autoComplete="given-name"
            required
            maxLength={100}
            value={firstName}
            onChange={(e) => setFirstName(e.target.value)}
            autoFocus
          />
          <TextField
            label="Фамилия"
            name="last_name"
            autoComplete="family-name"
            required
            maxLength={100}
            value={lastName}
            onChange={(e) => setLastName(e.target.value)}
          />
        </div>
        <TextField
          label="Почта"
          name="email"
          type="email"
          autoComplete="email"
          required
          maxLength={320}
          value={email}
          onChange={(e) => setEmail(e.target.value)}
          hint="Пришлём на неё код подтверждения."
        />
        <TextField
          label="Пароль"
          name="password"
          type="password"
          autoComplete="new-password"
          required
          maxLength={128}
          value={password}
          onChange={(e) => setPassword(e.target.value)}
          hint="Не короче 12 символов; удобнее — фраза из нескольких слов."
        />
        <TextField
          label="Повторите пароль"
          name="repeat"
          type="password"
          autoComplete="new-password"
          required
          maxLength={128}
          value={repeat}
          onChange={(e) => setRepeat(e.target.value)}
          error={touched && problem ? problem : undefined}
        />
        <div>
          <Checkbox
            label={
              <>
                Соглашаюсь на обработку персональных данных по{" "}
                <a href="/privacy" target="_blank" rel="noreferrer">
                  политике
                </a>
              </>
            }
            checked={consent}
            onChange={(e) => setConsent(e.target.checked)}
          />
          {touched && !consent ? (
            <p className={authStyles.fieldError} role="alert">
              Без согласия зарегистрироваться нельзя.
            </p>
          ) : null}
        </div>
        <Button type="submit" block busy={busy}>
          Зарегистрироваться
        </Button>
      </form>
      <p className={`muted ${authStyles.alt}`}>
        Уже есть учётная запись? <Link to={`/login${query ? `?${query}` : ""}`}>Войти</Link>
      </p>
    </AuthLayout>
  );
}

function RegisterError({ error }: { error: ApiError | string }) {
  if (typeof error === "string") return <Notice kind="error">{error}</Notice>;
  if (error.code === "registration_closed") {
    return (
      <Notice kind="info" title="Регистрация — по приглашению">
        Попросите ссылку или код у администратора вашей компании. Хотите подключить свою компанию —{" "}
        <Link to="/pricing/request">оставьте заявку</Link>.
      </Notice>
    );
  }
  return <Notice kind="error">{error.message}</Notice>;
}
