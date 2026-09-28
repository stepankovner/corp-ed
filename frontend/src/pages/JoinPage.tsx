import { useQuery } from "@tanstack/react-query";
import { useState, type ReactNode, type SubmitEvent } from "react";
import { Link, Navigate, useLocation, useParams } from "react-router";

import { api, unwrap } from "../api/client";
import { errorMessage } from "../api/errors";
import { useAuth } from "../auth/context";
import { passwordProblem } from "../auth/password";
import { formatDate } from "../lib/format";
import { Button } from "../ui/Button";
import { TextField } from "../ui/Field";
import { Notice } from "../ui/Notice";
import { PageSpinner } from "../ui/Spinner";
import authStyles from "./AuthLayout.module.css";
import { AuthLayout } from "./AuthLayout";

/**
 * Присоединение к компании по ссылке /join/<код>#<токен> (решение 28.09).
 * Токен — во фрагменте адреса: он не уходит на сервер и в журналы, в API
 * передаётся телом запроса.
 */
export function JoinPage() {
  const { companyCode = "" } = useParams();
  const token = useLocation().hash.replace(/^#/, "");
  const { state, logout } = useAuth();
  const [joining, setJoining] = useState(false);
  const preview = useQuery({
    queryKey: ["invite-preview", companyCode, token],
    queryFn: () =>
      unwrap(api.POST("/api/v1/invites/preview", { body: { company_code: companyCode, token } })),
    enabled: token.length >= 16,
    retry: false,
    staleTime: Infinity,
  });

  if (token.length < 16) {
    return (
      <JoinLayout title="Приглашение">
        <Notice kind="error">
          В ссылке нет кода приглашения. Скопируйте её целиком или попросите новую у администратора.
        </Notice>
      </JoinLayout>
    );
  }
  if (preview.isPending || state.status === "loading") return <PageSpinner />;
  if (preview.isError) {
    return (
      <JoinLayout title="Приглашение">
        <Notice kind="error">{errorMessage(preview.error)}</Notice>
        <Link to="/login">Ко входу</Link>
      </JoinLayout>
    );
  }

  const company = preview.data.company_name;
  if (state.status === "authenticated") {
    // Уже в этой компании — только что присоединился или открыл ссылку
    // повторно: сразу к вопросам.
    if (state.user.company_code === companyCode.toLowerCase()) {
      return <Navigate to="/" replace />;
    }
    return (
      <JoinLayout title={`Приглашение в «${company}»`}>
        <Notice kind="info">
          Сейчас вы вошли в «{state.user.company_name}» как {state.user.email}. Чтобы присоединиться
          к «{company}», выйдите из текущей учётки.
        </Notice>
        <Button block onClick={() => void logout()}>
          Выйти и продолжить
        </Button>
      </JoinLayout>
    );
  }

  if (!joining) {
    return (
      <JoinLayout
        title="Присоединиться к команде"
        lead={`«${company}» приглашает вас в Kronto — ассистента, который отвечает по документам компании со ссылкой на источник.`}
      >
        <p className="muted">Ссылка действует до {formatDate(preview.data.expires_at)}.</p>
        <Button block onClick={() => setJoining(true)}>
          Присоединиться
        </Button>
        <p className="muted">
          Уже есть учётка? <Link to="/login">Войти</Link>
        </p>
      </JoinLayout>
    );
  }
  return (
    <JoinForm
      company={company}
      companyCode={companyCode}
      token={token}
      emailDomain={preview.data.email_domain}
    />
  );
}

function JoinLayout({
  title,
  lead,
  children,
}: {
  title: string;
  lead?: string;
  children: ReactNode;
}) {
  return (
    <AuthLayout bar="Kronto" title={title} lead={lead}>
      <div className={authStyles.form}>{children}</div>
    </AuthLayout>
  );
}

function JoinForm({
  company,
  companyCode,
  token,
  emailDomain,
}: {
  company: string;
  companyCode: string;
  token: string;
  emailDomain: string | null;
}) {
  const { acceptInvite } = useAuth();
  const [email, setEmail] = useState("");
  const [fullName, setFullName] = useState("");
  const [password, setPassword] = useState("");
  const [repeat, setRepeat] = useState("");
  const [touched, setTouched] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const problem = passwordProblem(password, repeat, email);

  async function submit(event: SubmitEvent) {
    event.preventDefault();
    setTouched(true);
    if (problem) return;
    setError(null);
    setBusy(true);
    try {
      await acceptInvite({
        company: companyCode,
        token,
        email: email.trim(),
        fullName: fullName.trim(),
        password,
      });
      // Дальше JoinPage увидит вход в эту компанию и уведёт к вопросам.
    } catch (err) {
      setError(errorMessage(err));
      setBusy(false);
    }
  }

  return (
    <AuthLayout
      bar={company}
      title="Ваша учётка"
      lead="Почта и пароль — для входа в Kronto. Код компании запомним сами."
    >
      <form className={authStyles.form} onSubmit={submit}>
        {error ? <Notice kind="error">{error}</Notice> : null}
        <TextField
          label="Рабочая почта"
          type="email"
          autoComplete="username"
          required
          value={email}
          onChange={(e) => setEmail(e.target.value)}
          hint={emailDomain ? `Только почта @${emailDomain}.` : undefined}
          autoFocus
        />
        <TextField
          label="Имя и фамилия"
          optional
          autoComplete="name"
          maxLength={200}
          value={fullName}
          onChange={(e) => setFullName(e.target.value)}
        />
        <TextField
          label="Пароль"
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
          type="password"
          autoComplete="new-password"
          required
          maxLength={128}
          value={repeat}
          onChange={(e) => setRepeat(e.target.value)}
          error={touched && problem ? problem : undefined}
        />
        <Button type="submit" block busy={busy}>
          Присоединиться
        </Button>
      </form>
    </AuthLayout>
  );
}
