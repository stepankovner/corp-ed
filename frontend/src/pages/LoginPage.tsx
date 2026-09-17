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
      <section className={styles.pitch}>
        <p className={styles.brand}>corp-ed</p>
        <h1 className="display">
          Адаптация стажёров,
          <br />
          <span className="display-muted">собранная из ваших материалов.</span>
        </h1>
        <p className={styles.lead}>
          Программа 30/60/90 по брифу руководителя и ответы на вопросы стажёра
          строго по документам компании — без выдумок.
        </p>

        <div className={styles.points}>
          <div className={styles.point}>
            <span className={styles.pointNumber}>01</span>
            <span>Материалы отдела превращаются в базу знаний.</span>
          </div>
          <div className={styles.point}>
            <span className={styles.pointNumber}>02</span>
            <span>Бриф руководителя — в программу адаптации.</span>
          </div>
          <div className={styles.point}>
            <span className={styles.pointNumber}>03</span>
            <span>Нет ответа в материалах — бот честно скажет об этом.</span>
          </div>
        </div>
      </section>

      <section className={styles.card}>
        <div className="stack gap-8">
          <p className="eyebrow">Вход</p>
          <h2 className="section-title">Войдите в рабочее пространство</h2>
        </div>

        {error ? <Notice tone="error">{errorMessage(error)}</Notice> : null}

        <form className={styles.form} onSubmit={handleSubmit} noValidate>
          <TextField
            label="Код компании"
            value={companyCode}
            onChange={(event) => setCompanyCode(event.target.value)}
            error={fieldErrors.company_code}
            autoComplete="organization"
            placeholder="demo"
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

          <Button className={styles.submit} type="submit" loading={pending}>
            {pending ? "Входим" : "Войти"}
          </Button>
        </form>
      </section>
    </div>
  );
}
