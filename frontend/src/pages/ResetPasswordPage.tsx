import { useState, type SubmitEvent } from "react";
import { Link, useNavigate } from "react-router";

import { api, unwrap } from "../api/client";
import { ApiError, errorMessage } from "../api/errors";
import { useAuth } from "../auth/context";
import { useHashSecret } from "../auth/hashSecret";
import { passwordProblem } from "../auth/password";
import { pendingInvite } from "../auth/pendingInvite";
import { useDocumentTitle } from "../lib/title";
import { Button } from "../ui/Button";
import { TextField } from "../ui/Field";
import { Notice } from "../ui/Notice";
import authStyles from "./AuthLayout.module.css";
import { AuthLayout } from "./AuthLayout";

/**
 * Новый пароль по ссылке /reset-password#token=… (ТЗ §3). У кого включено
 * приложение или ключ доступа, сервер попросит ещё код приложения или
 * резервный: иначе взлом почты обходил бы второй фактор. Прежние сеансы
 * закрываются, открывается новый.
 */
export function ResetPasswordPage() {
  useDocumentTitle("Новый пароль");
  const token = useHashSecret("token");
  const { signIn } = useAuth();
  const navigate = useNavigate();
  const [password, setPassword] = useState("");
  const [repeat, setRepeat] = useState("");
  const [secondFactor, setSecondFactor] = useState<string | null>(null);
  const [touched, setTouched] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [linkDead, setLinkDead] = useState(false);
  const [busy, setBusy] = useState(false);
  // Почту здесь не знаем — проверка «пароль без почты» за сервером.
  const problem = passwordProblem(password, repeat, "");

  if (!token || linkDead) {
    return (
      <AuthLayout bar="kronto" title="Ссылка не сработала">
        <div className={authStyles.form}>
          <Notice kind="error">
            {linkDead
              ? (error ?? "Ссылка недействительна.")
              : "В адресе нет ссылки из письма. Откройте ссылку из письма целиком."}
          </Notice>
          <Link to="/forgot-password">Отправить новую ссылку</Link>
        </div>
      </AuthLayout>
    );
  }

  async function submit(event: SubmitEvent) {
    event.preventDefault();
    setTouched(true);
    if (problem || !token) return;
    setError(null);
    setBusy(true);
    try {
      const tokens = await unwrap(
        api.POST("/api/v1/auth/reset-password", {
          body: {
            token,
            new_password: password,
            second_factor: secondFactor?.trim() || null,
          },
        }),
      );
      await signIn(tokens);
      void navigate(pendingInvite() ? "/join" : "/", { replace: true });
    } catch (err) {
      setBusy(false);
      if (err instanceof ApiError && err.code === "second_factor_required") {
        setSecondFactor("");
        return;
      }
      if (err instanceof ApiError && err.code === "invalid_code") {
        setError(err.message);
        setLinkDead(true);
        return;
      }
      setError(errorMessage(err));
    }
  }

  return (
    <AuthLayout
      bar="kronto"
      title="Задайте новый пароль"
      lead="После сохранения остальные сеансы завершатся, а вы войдёте здесь."
    >
      <form className={authStyles.form} onSubmit={submit}>
        {error ? <Notice kind="error">{error}</Notice> : null}
        <TextField
          label="Новый пароль"
          name="password"
          type="password"
          autoComplete="new-password"
          required
          maxLength={128}
          value={password}
          onChange={(e) => setPassword(e.target.value)}
          hint="Не короче 12 символов; удобнее — фраза из нескольких слов."
          autoFocus
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
        {secondFactor !== null ? (
          <>
            <Notice kind="info">
              У вас включена защита входа: введите код из приложения-аутентификатора или один из
              резервных кодов.
            </Notice>
            <TextField
              label="Код из приложения или резервный"
              name="second_factor"
              className={authStyles.code}
              autoComplete="one-time-code"
              autoCapitalize="characters"
              spellCheck={false}
              required
              maxLength={12}
              value={secondFactor}
              onChange={(e) => setSecondFactor(e.target.value)}
              autoFocus
            />
          </>
        ) : null}
        <Button type="submit" block busy={busy}>
          Сохранить и войти
        </Button>
      </form>
    </AuthLayout>
  );
}
