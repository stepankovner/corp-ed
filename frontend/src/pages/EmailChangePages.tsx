import { CircleCheck } from "lucide-react";
import { useEffect, useRef, useState, type ReactNode } from "react";
import { Link } from "react-router";

import { api, unwrap } from "../api/client";
import { errorMessage } from "../api/errors";
import { useAuth } from "../auth/context";
import { useHashSecret } from "../auth/hashSecret";
import { useDocumentTitle } from "../lib/title";
import { Notice } from "../ui/Notice";
import { PageSpinner } from "../ui/Spinner";
import authStyles from "./AuthLayout.module.css";
import { AuthLayout } from "./AuthLayout";

type Outcome = { status: "pending" } | { status: "done" } | { status: "failed"; message: string };

/**
 * Одноразовая ссылка из письма: один запрос, даже в StrictMode. ready —
 * сессия этой вкладки уже восстановлена: знаем, вошли ли здесь.
 */
function useOneShot(
  token: string | null,
  ready: boolean,
  run: (token: string) => Promise<void>,
): Outcome {
  const [outcome, setOutcome] = useState<Outcome>(() =>
    token
      ? { status: "pending" }
      : { status: "failed", message: "В адресе нет ссылки из письма. Откройте её целиком." },
  );
  const started = useRef(false);
  useEffect(() => {
    if (!token || !ready || started.current) return;
    started.current = true;
    run(token).then(
      () => setOutcome({ status: "done" }),
      (err: unknown) => setOutcome({ status: "failed", message: errorMessage(err) }),
    );
  }, [token, ready, run]);
  return outcome;
}

function Result({ title, children }: { title: string; children: ReactNode }) {
  return (
    <AuthLayout bar="kronto" title={title}>
      <div className={authStyles.form}>{children}</div>
    </AuthLayout>
  );
}

/** Ссылка на новый адрес (/confirm-email#token=…): почта меняется после перехода. */
export function ConfirmEmailPage() {
  useDocumentTitle("Смена почты");
  const token = useHashSecret("token");
  const { state, reloadMe } = useAuth();
  const signedIn = state.status === "authenticated";
  const outcome = useOneShot(token, state.status !== "loading", async (value) => {
    await unwrap(api.POST("/api/v1/account/email/confirm", { body: { token: value } }));
    // Вошли в этом браузере — в профиле уже новая почта.
    if (signedIn) await reloadMe();
  });

  if (outcome.status === "pending") return <PageSpinner />;
  if (outcome.status === "failed") {
    return (
      <Result title="Ссылка не сработала">
        <Notice kind="error">{outcome.message}</Notice>
        <Link to={signedIn ? "/settings/account" : "/login"}>
          {signedIn ? "К настройкам учётной записи" : "Ко входу"}
        </Link>
      </Result>
    );
  }
  return (
    <Result title="Почта изменена">
      <div className={authStyles.result}>
        <CircleCheck size={40} aria-hidden />
        <p>Теперь входите с новым адресом. На прежний мы отправили уведомление.</p>
      </div>
      <Link to={signedIn ? "/" : "/login"}>{signedIn ? "Вернуться в kronto" : "Войти"}</Link>
    </Result>
  );
}

/**
 * «Это не я» из письма на прежний адрес (/revert-email#token=…): почта
 * возвращается, все сеансы закрываются, на прежний адрес уходит ссылка
 * для нового пароля — тот, кто сменил почту, мог знать и пароль.
 */
export function RevertEmailPage() {
  useDocumentTitle("Возврат почты");
  const token = useHashSecret("token");
  const { state, logout } = useAuth();
  const outcome = useOneShot(token, state.status !== "loading", async (value) => {
    await unwrap(api.POST("/api/v1/account/email/revert", { body: { token: value } }));
    // Сеансы на сервере уже закрыты — здесь тоже выходим.
    await logout();
  });

  if (outcome.status === "pending") return <PageSpinner />;
  if (outcome.status === "failed") {
    return (
      <Result title="Ссылка не сработала">
        <Notice kind="error">{outcome.message}</Notice>
        <p className="muted">
          Если кто-то сменил почту без вас, напишите в поддержку — поможем вернуть доступ.
        </p>
        <Link to="/login">Ко входу</Link>
      </Result>
    );
  }
  return (
    <Result title="Почта возвращена">
      <div className={authStyles.result}>
        <CircleCheck size={40} aria-hidden />
        <p>
          Мы вернули прежний адрес и завершили все сеансы. На него отправили ссылку для нового
          пароля — задайте его, прежний мог быть известен постороннему.
        </p>
      </div>
      <Link to="/login">Ко входу</Link>
    </Result>
  );
}
