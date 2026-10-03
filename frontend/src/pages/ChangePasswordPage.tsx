import { useState, type SubmitEvent } from "react";
import { Link, Navigate } from "react-router";

import { errorMessage } from "../api/errors";
import { useAuth, useMe } from "../auth/context";
import { MIN_PASSWORD, passwordProblem } from "../auth/password";
import { useDocumentTitle } from "../lib/title";
import { Button } from "../ui/Button";
import { TextField } from "../ui/Field";
import { Notice } from "../ui/Notice";
import authStyles from "./AuthLayout.module.css";
import { AuthLayout } from "./AuthLayout";

export function ChangePasswordPage() {
  const me = useMe();
  const { changePassword, logout } = useAuth();
  const forced = me.must_change_password;
  useDocumentTitle(forced ? "Задайте свой пароль" : "Смена пароля");
  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const [repeat, setRepeat] = useState("");
  const [touched, setTouched] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [done, setDone] = useState(false);
  const problem = passwordProblem(next, repeat, me.email);

  // Уходим, когда профиль уже без временного пароля: иначе охранник
  // маршрута вернёт на эту страницу по устаревшему профилю.
  if (done && !me.must_change_password) return <Navigate to="/" replace />;

  async function submit(event: SubmitEvent) {
    event.preventDefault();
    setTouched(true);
    if (problem) return;
    setError(null);
    setBusy(true);
    try {
      await changePassword(current, next);
      setDone(true);
    } catch (err) {
      setError(errorMessage(err));
      setBusy(false);
    }
  }

  return (
    <AuthLayout
      bar={me.company_name}
      title={forced ? "Задайте свой пароль" : "Смена пароля"}
      lead={
        forced
          ? "Вы вошли по временному паролю от администратора. Придумайте свой — временный он знает."
          : "После смены пароля остальные сеансы завершатся."
      }
    >
      <form className={authStyles.form} onSubmit={submit}>
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
          label={forced ? "Временный пароль" : "Текущий пароль"}
          type="password"
          autoComplete="current-password"
          required
          value={current}
          onChange={(e) => setCurrent(e.target.value)}
          autoFocus
        />
        <TextField
          label="Новый пароль"
          type="password"
          autoComplete="new-password"
          required
          value={next}
          onChange={(e) => setNext(e.target.value)}
          hint={`Не короче ${MIN_PASSWORD} символов. Длинная фраза надёжнее сложного короткого пароля.`}
        />
        <TextField
          label="Повторите новый пароль"
          type="password"
          autoComplete="new-password"
          required
          value={repeat}
          onChange={(e) => setRepeat(e.target.value)}
          error={touched ? problem : null}
        />
        <Button type="submit" block busy={busy}>
          Сохранить пароль
        </Button>
        {forced ? (
          <Button variant="link" onClick={() => void logout()}>
            Выйти
          </Button>
        ) : (
          <Link to="/" style={{ textAlign: "center", fontSize: "var(--fs-small)" }}>
            Отмена
          </Link>
        )}
      </form>
    </AuthLayout>
  );
}
