import { KeyRound } from "lucide-react";
import { useState, type SubmitEvent } from "react";

import { api, unwrap } from "../api/client";
import { ApiError, errorMessage } from "../api/errors";
import { useAuth, type MfaChallenge, type MfaMethod } from "../auth/context";
import { useDocumentTitle } from "../lib/title";
import { passkeysSupported, PasskeyError, requestPasskey } from "../lib/webauthn";
import { Button } from "../ui/Button";
import { TextField } from "../ui/Field";
import { Notice } from "../ui/Notice";
import authStyles from "./AuthLayout.module.css";
import { AuthLayout } from "./AuthLayout";
import { ResendLink } from "./ResendLink";

const TITLES: Record<MfaMethod, string> = {
  email: "Код из письма",
  totp: "Код из приложения",
  passkey: "Ключ доступа",
  backup: "Резервный код",
};

const SWITCH_LABELS: Record<MfaMethod, string> = {
  email: "Код на почту",
  totp: "Код из приложения",
  passkey: "Ключ доступа",
  backup: "Резервный код",
};

function firstMethod(methods: MfaMethod[]): MfaMethod {
  if (methods.includes("totp")) return "totp";
  if (methods.includes("passkey") && passkeysSupported()) return "passkey";
  return methods[0] ?? "email";
}

/** Второй шаг входа: способ выбирает человек из тех, что есть у учётки. */
export function SecondFactorStep({
  challenge,
  onRestart,
  onBack,
}: {
  challenge: MfaChallenge;
  /** Шаг истёк или попытки кончились — снова пароль. */
  onRestart: () => void;
  onBack: () => void;
}) {
  useDocumentTitle("Подтверждение входа");
  const [method, setMethod] = useState<MfaMethod>(() => firstMethod(challenge.methods));
  const others = challenge.methods.filter(
    (item) => item !== method && (item !== "passkey" || passkeysSupported()),
  );

  return (
    <AuthLayout bar="kronto" title={TITLES[method]} lead={lead(method, challenge.email_hint)}>
      {method === "passkey" ? (
        <PasskeyForm token={challenge.token} onRestart={onRestart} />
      ) : (
        <CodeForm key={method} token={challenge.token} method={method} onRestart={onRestart} />
      )}
      <div className={authStyles.links}>
        <button type="button" className={authStyles.linkButton} onClick={onBack}>
          Назад
        </button>
        {others.map((item) => (
          <button
            key={item}
            type="button"
            className={authStyles.linkButton}
            onClick={() => setMethod(item)}
          >
            {SWITCH_LABELS[item]}
          </button>
        ))}
      </div>
    </AuthLayout>
  );
}

function lead(method: MfaMethod, emailHint: string | null): string {
  switch (method) {
    case "email":
      return `Отправили 6 цифр на ${emailHint ?? "вашу почту"}. Код действует 10 минут.`;
    case "totp":
      return "Откройте приложение-аутентификатор и введите 6 цифр для kronto.";
    case "backup":
      return "Один из 10 кодов, сохранённых при настройке защиты. Каждый срабатывает один раз.";
    case "passkey":
      return "Подтвердите вход отпечатком, лицом, PIN-кодом устройства или ключом безопасности.";
  }
}

/** Ошибка шага: истёк — заново с паролем, иначе — текст под полем. */
function useStepError(onRestart: () => void) {
  const [error, setError] = useState<string | null>(null);
  function fail(err: unknown) {
    if (err instanceof ApiError && err.code === "login_expired") {
      onRestart();
      return;
    }
    setError(err instanceof PasskeyError ? err.message : errorMessage(err));
  }
  return { error, setError, fail };
}

function CodeForm({
  token,
  method,
  onRestart,
}: {
  token: string;
  method: Exclude<MfaMethod, "passkey">;
  onRestart: () => void;
}) {
  const { verifySecondFactor } = useAuth();
  const [code, setCode] = useState("");
  const [busy, setBusy] = useState(false);
  const { error, setError, fail } = useStepError(onRestart);
  const numeric = method !== "backup";

  async function verify(value: string) {
    setError(null);
    setBusy(true);
    try {
      await verifySecondFactor({ token, method, code: value });
      // Вошли — PublicOnly уведёт дальше.
    } catch (err) {
      setCode("");
      setBusy(false);
      fail(err);
    }
  }

  function submit(event: SubmitEvent) {
    event.preventDefault();
    void verify(code.trim());
  }

  function change(value: string) {
    const next = numeric ? value.replace(/\D/g, "").slice(0, 6) : value.toUpperCase();
    setCode(next);
    // Шесть цифр — отправляем сами, как в банковских приложениях.
    if (numeric && next.length === 6 && !busy) void verify(next);
  }

  return (
    <form className={authStyles.form} onSubmit={submit}>
      {error ? <Notice kind="error">{error}</Notice> : null}
      <TextField
        label={numeric ? "Код из 6 цифр" : "Код вида K7QM-4XPA"}
        name="code"
        className={authStyles.code}
        inputMode={numeric ? "numeric" : "text"}
        autoComplete="one-time-code"
        autoCapitalize="characters"
        spellCheck={false}
        required
        maxLength={numeric ? 6 : 12}
        value={code}
        onChange={(e) => change(e.target.value)}
        autoFocus
      />
      <Button type="submit" block busy={busy}>
        Подтвердить
      </Button>
      {method === "email" ? (
        <ResendLink
          label="Отправить код ещё раз"
          onResend={() =>
            unwrap(api.POST("/api/v1/auth/mfa/resend", { body: { token } })).catch(
              (err: unknown) => {
                fail(err);
                throw err;
              },
            )
          }
        />
      ) : null}
    </form>
  );
}

function PasskeyForm({ token, onRestart }: { token: string; onRestart: () => void }) {
  const { verifySecondFactor } = useAuth();
  const [busy, setBusy] = useState(false);
  const { error, setError, fail } = useStepError(onRestart);

  async function start() {
    setError(null);
    setBusy(true);
    try {
      const { options } = await unwrap(
        api.POST("/api/v1/auth/mfa/passkey-options", { body: { token } }),
      );
      const credential = await requestPasskey(options);
      await verifySecondFactor({ token, method: "passkey", credential });
    } catch (err) {
      setBusy(false);
      fail(err);
    }
  }

  return (
    <div className={authStyles.form}>
      {error ? <Notice kind="error">{error}</Notice> : null}
      <Button block busy={busy} onClick={() => void start()} autoFocus>
        <KeyRound size={18} aria-hidden /> Войти с ключом доступа
      </Button>
    </div>
  );
}
