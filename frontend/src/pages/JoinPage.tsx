import { useQuery } from "@tanstack/react-query";
import { useEffect, useState, type ReactNode, type SubmitEvent } from "react";
import { Link, useNavigate } from "react-router";

import { api, unwrap, type Schemas } from "../api/client";
import { errorMessage } from "../api/errors";
import { useAuth, type Me } from "../auth/context";
import { useHashSecret } from "../auth/hashSecret";
import {
  clearPendingInvite,
  inviteSecretFrom,
  pendingInvite,
  savePendingInvite,
} from "../auth/pendingInvite";
import { formatDate } from "../lib/format";
import { useDocumentTitle } from "../lib/title";
import { Button } from "../ui/Button";
import { buttonClass } from "../ui/buttonClass";
import { TextField } from "../ui/Field";
import { Notice } from "../ui/Notice";
import { PageSpinner } from "../ui/Spinner";
import { useToast } from "../ui/useToast";
import authStyles from "./AuthLayout.module.css";
import { AuthLayout } from "./AuthLayout";

type Preview = Schemas["InvitePreviewResponse"];

/**
 * Вступление в компанию (ТЗ §2): по ссылке /join#<токен> или по коду
 * вида K7QM-4XPA. Без учётки — регистрация или вход, приглашение ждёт в
 * памяти страницы (auth/pendingInvite.ts); с учёткой — одна кнопка. Если в компании
 * включено одобрение, человек ждёт администратора.
 */
export function JoinPage() {
  useDocumentTitle("Приглашение");
  const fromHash = useHashSecret(null);
  const [secret, setSecret] = useState(() => fromHash ?? pendingInvite());
  const { state } = useAuth();

  useEffect(() => {
    if (fromHash) savePendingInvite(fromHash);
  }, [fromHash]);

  const preview = useQuery({
    queryKey: ["invite-preview", secret],
    queryFn: () => unwrap(api.POST("/api/v1/invites/preview", { body: { secret: secret ?? "" } })),
    enabled: secret !== null,
    retry: false,
    staleTime: Infinity,
  });

  function choose(value: string | null) {
    if (value) savePendingInvite(value);
    else clearPendingInvite();
    setSecret(value);
  }

  if (!secret) return <EnterInvite onEnter={choose} />;
  if (preview.isPending || state.status === "loading") return <PageSpinner />;
  if (preview.isError) {
    return (
      <Card title="Приглашение не найдено">
        <Notice kind="error">{errorMessage(preview.error)}</Notice>
        <Button block variant="ghost" onClick={() => choose(null)}>
          Ввести другую ссылку или код
        </Button>
      </Card>
    );
  }
  if (state.status === "authenticated") {
    return <Accept secret={secret} preview={preview.data} me={state.user} />;
  }
  return <SignUpFirst preview={preview.data} />;
}

function Card({
  title,
  lead,
  bar = "kronto",
  children,
}: {
  title: string;
  lead?: string;
  bar?: string;
  children: ReactNode;
}) {
  return (
    <AuthLayout bar={bar} title={title} lead={lead}>
      <div className={authStyles.form}>{children}</div>
    </AuthLayout>
  );
}

function EnterInvite({ onEnter }: { onEnter: (secret: string) => void }) {
  const [value, setValue] = useState("");
  function submit(event: SubmitEvent) {
    event.preventDefault();
    const secret = inviteSecretFrom(value);
    if (secret) onEnter(secret);
  }
  return (
    <Card title="Вступить в компанию" lead="Ссылку или код даёт администратор вашей компании.">
      <form className={authStyles.form} onSubmit={submit}>
        <TextField
          label="Ссылка или код приглашения"
          name="invite"
          autoComplete="off"
          autoCapitalize="characters"
          spellCheck={false}
          required
          maxLength={512}
          placeholder="K7QM-4XPA"
          value={value}
          onChange={(e) => setValue(e.target.value)}
          autoFocus
        />
        <Button type="submit" block>
          Продолжить
        </Button>
      </form>
    </Card>
  );
}

function Terms({ preview }: { preview: Preview }) {
  return (
    <ul className={`muted ${authStyles.terms}`}>
      <li>Приглашение действует до {formatDate(preview.expires_at)}.</li>
      {preview.email_domain ? <li>Только для почты @{preview.email_domain}.</li> : null}
      {preview.requires_approval ? (
        <li>Администратор компании подтвердит вступление — обычно в течение дня.</li>
      ) : null}
    </ul>
  );
}

/** Без учётки: сначала регистрация или вход, приглашение ждёт во вкладке. */
function SignUpFirst({ preview }: { preview: Preview }) {
  return (
    <Card
      bar={preview.company_name}
      title="Присоединиться к команде"
      lead={`«${preview.company_name}» приглашает вас в kronto — ассистента, который отвечает по документам компании со ссылкой на источник.`}
    >
      <Terms preview={preview} />
      <Link to="/register?next=%2Fjoin" className={buttonClass("dark", "md", true)}>
        Создать учётную запись
      </Link>
      <Link to="/login?next=%2Fjoin" className={buttonClass("ghost", "md", true)}>
        У меня есть учётная запись
      </Link>
    </Card>
  );
}

function Accept({ secret, preview, me }: { secret: string; preview: Preview; me: Me }) {
  const { signIn, reloadMe, logout } = useAuth();
  const navigate = useNavigate();
  const toast = useToast();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [waiting, setWaiting] = useState(false);
  const company = preview.company_name;
  const domain = preview.email_domain;
  const wrongDomain = domain !== null && !me.email.toLowerCase().endsWith(`@${domain}`);

  async function join() {
    setError(null);
    setBusy(true);
    try {
      const result = await unwrap(api.POST("/api/v1/invites/accept", { body: { secret } }));
      clearPendingInvite();
      if (!result.session) {
        await reloadMe();
        setWaiting(true);
        return;
      }
      await signIn(result.session);
      toast.show(
        result.outcome === "already_member"
          ? `Вы уже в «${result.company_name}» — переключили на неё`
          : `Вы в «${result.company_name}»`,
        { tone: "success" },
      );
      void navigate("/", { replace: true });
    } catch (err) {
      setError(errorMessage(err));
      setBusy(false);
    }
  }

  if (waiting) {
    return (
      <Card title="Заявка отправлена" bar={company}>
        <p>
          Администратор «{company}» увидит её в разделе «Люди». Как только он одобрит, компания
          станет доступна в переключателе компаний слева.
        </p>
        <Link to="/" className={buttonClass("dark", "md", true)}>
          На главную
        </Link>
      </Card>
    );
  }

  return (
    <Card
      bar={company}
      title={`Вступить в «${company}»`}
      lead="kronto отвечает по документам компании со ссылкой на источник."
    >
      {error ? <Notice kind="error">{error}</Notice> : null}
      {wrongDomain ? (
        <Notice kind="warn">
          Приглашение только для почты @{domain}, а вы вошли как {me.email}. Войдите другой учётной
          записью или попросите у администратора другое приглашение.
        </Notice>
      ) : null}
      <Terms preview={preview} />
      <Button block busy={busy} onClick={() => void join()} disabled={wrongDomain}>
        {preview.requires_approval ? "Отправить заявку" : "Вступить"}
      </Button>
      <p className={`muted ${authStyles.alt}`}>
        Вы вошли как {me.email}.{" "}
        <button type="button" className={authStyles.linkButton} onClick={() => void logout()}>
          Войти другой учётной записью
        </button>
      </p>
    </Card>
  );
}
