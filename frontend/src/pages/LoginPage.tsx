import { useState, type FormEvent } from "react";
import { Navigate, useLocation, useNavigate } from "react-router-dom";

import { errorMessage, fieldErrorsOf } from "../api/ApiError";
import { useAuth } from "../auth/AuthContext";
import { Button } from "../components/Button";
import { TextField } from "../components/Field";
import { Notice } from "../components/Notice";
import styles from "./LoginPage.module.css";

export function LoginPage() {
  const { status, login } = useAuth();
  const navigate = useNavigate();
  const location = useLocation();

  const [companyCode, setCompanyCode] = useState("");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<unknown>(null);
  const [pending, setPending] = useState(false);

  if (status === "authenticated") {
    return <Navigate to="/" replace />;
  }

  const fieldErrors = fieldErrorsOf(error);

  async function handleSubmit(event: FormEvent) {
    event.preventDefault();
    setError(null);
    setPending(true);

    try {
      const me = await login(companyCode.trim(), email.trim(), password);
      const from = (location.state as { from?: string } | null)?.from;
      const home = me.role === "manager" ? "/materials" : "/chat";
      navigate(from ?? home, { replace: true });
    } catch (caught) {
      setError(caught);
    } finally {
      setPending(false);
    }
  }

  return (
    <div className={styles.page}>
      <div className={styles.column}>
        <div className={styles.brand}>
          <span className={styles.wordmark}>kronto</span>
          <span className="meta">адаптация стажёров · вход для сотрудников</span>
        </div>

        <section className={styles.panel}>
          {error ? <Notice tone="error">{errorMessage(error)}</Notice> : null}

          <form className={styles.form} onSubmit={handleSubmit} noValidate>
            <TextField
              label="Код компании"
              value={companyCode}
              onChange={(event) => setCompanyCode(event.target.value)}
              error={fieldErrors.company_code}
              autoComplete="organization"
              required
            />
            <TextField
              label="Рабочая почта"
              type="email"
              value={email}
              onChange={(event) => setEmail(event.target.value)}
              error={fieldErrors.email}
              autoComplete="username"
              required
            />
            <TextField
              label="Пароль"
              type="password"
              value={password}
              onChange={(event) => setPassword(event.target.value)}
              error={fieldErrors.password}
              autoComplete="current-password"
              required
            />

            <Button className={styles.submit} type="submit" loading={pending}>
              {pending ? "Входим" : "Войти"}
            </Button>
          </form>

          <p className={styles.footnote}>
            Код компании выдаёт администратор вашей организации. Пароль
            восстанавливает он же.
          </p>
        </section>
      </div>
    </div>
  );
}
