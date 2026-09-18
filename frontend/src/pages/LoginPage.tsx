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
      const home = me.role === "manager" ? "/materials" : "/my";
      navigate(from ?? home, { replace: true });
    } catch (caught) {
      setError(caught);
    } finally {
      setPending(false);
    }
  }

  return (
    <div className={styles.page}>
      <header className={styles.head}>
        <span className={styles.wordmark}>kronto</span>
        <span className="label">адаптация стажёров</span>
      </header>

      <div className={styles.body}>
        <div className={styles.column}>
          <div className={styles.intro}>
            <h1 className="h1">Вход в рабочее пространство</h1>
            <p className="lead">
              Код компании выдаёт администратор. Доступ зависит от роли.
            </p>
          </div>

          <section className={`card ${styles.card}`}>
            <form className="stack gap-16" onSubmit={handleSubmit} noValidate>
              <TextField
                label="Код компании"
                value={companyCode}
                onChange={(event) => setCompanyCode(event.target.value)}
                error={fieldErrors.company_code}
                autoComplete="organization"
                required
              />
              <TextField
                label="Email"
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

              {error ? (
                <Notice tone="error">{errorMessage(error)}</Notice>
              ) : null}

              <Button className={styles.submit} type="submit" loading={pending}>
                {pending ? "Входим" : "Войти"}
              </Button>
            </form>
          </section>
        </div>
      </div>
    </div>
  );
}
