import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useId, useState, type SubmitEvent } from "react";

import { CopyButton } from "../admin/common";
import { api, unwrap, type Schemas } from "../api/client";
import { ApiError, errorMessage } from "../api/errors";
import { useAuth, useMe } from "../auth/context";
import { useMediaQuery } from "../lib/media";
import { Badge } from "../ui/Badge";
import { Button } from "../ui/Button";
import { TextField } from "../ui/Field";
import { Modal } from "../ui/Modal";
import { Notice } from "../ui/Notice";
import { useToast } from "../ui/useToast";
import { Section } from "./common";
import { SECURITY_KEY } from "./keys";
import { QrCode } from "./QrCode";
import styles from "./Settings.module.css";

type Security = Schemas["SecurityResponse"];
type Setup = Schemas["TotpSetupResponse"];

/** Ключ для ручного ввода — группами по 4, как его показывают приложения. */
function groupSecret(secret: string): string {
  return secret.replace(/(.{4})(?=.)/g, "$1 ");
}

export function TotpSection({
  security,
  onCodes,
}: {
  security: Security;
  onCodes: (codes: string[] | null) => void;
}) {
  const { reloadMe } = useAuth();
  const queryClient = useQueryClient();
  const toast = useToast();
  const [setup, setSetup] = useState<Setup | null>(null);
  const [disabling, setDisabling] = useState(false);
  const start = useMutation({
    mutationFn: () => unwrap(api.POST("/api/v1/account/totp/setup")),
    onSuccess: setSetup,
  });
  // Единственный надёжный способ, а он обязателен: сервер отключить не даст.
  const locked = security.strong_required && security.passkeys.length === 0;

  // Надёжный фактор появился или пропал — профиль (mfa) и раздел перечитать.
  async function refresh() {
    await Promise.all([reloadMe(), queryClient.invalidateQueries({ queryKey: SECURITY_KEY })]);
  }

  return (
    <Section
      title="Приложение-аутентификатор"
      description="Яндекс Ключ, Google Authenticator или другое приложение с одноразовыми кодами: при входе вы вводите 6 цифр из него."
      aside={security.totp_enabled ? <Badge tone="ok">включено</Badge> : <Badge>выключено</Badge>}
    >
      {start.isError ? <Notice kind="error">{errorMessage(start.error)}</Notice> : null}
      {security.totp_enabled && locked ? (
        <p className={`muted ${styles.small}`}>
          Это ваш единственный надёжный способ входа, а в ваших компаниях он обязателен. Чтобы
          отключить приложение, сначала добавьте ключ доступа.
        </p>
      ) : null}
      <div className={styles.actions}>
        {security.totp_enabled ? (
          <Button variant="ghost" size="sm" disabled={locked} onClick={() => setDisabling(true)}>
            Отключить приложение
          </Button>
        ) : (
          <Button size="sm" busy={start.isPending} onClick={() => start.mutate()}>
            Подключить приложение
          </Button>
        )}
      </div>
      {setup ? (
        <TotpSetupDialog
          setup={setup}
          onClose={() => setSetup(null)}
          onExpired={() => {
            // Секрет настройки сгорел — новый секрет и новый QR-код.
            setSetup(null);
            toast.show("Время настройки вышло — отсканируйте новый QR-код", { tone: "info" });
            start.mutate();
          }}
          onEnabled={async (codes) => {
            setSetup(null);
            onCodes(codes);
            toast.show("Приложение подключено");
            await refresh();
          }}
        />
      ) : null}
      {disabling ? (
        <TotpDisableDialog
          onClose={() => setDisabling(false)}
          onDisabled={async () => {
            setDisabling(false);
            toast.show("Приложение отключено");
            await refresh();
          }}
        />
      ) : null}
    </Section>
  );
}

function TotpSetupDialog({
  setup,
  onClose,
  onExpired,
  onEnabled,
}: {
  setup: Setup;
  onClose: () => void;
  /** Настройка истекла или исчерпала попытки (setup_expired). */
  onExpired: () => void;
  onEnabled: (codes: string[] | null) => Promise<void>;
}) {
  const formId = useId();
  // Отсканировать экран телефона самим телефоном нельзя — там ссылка в приложение.
  const touch = useMediaQuery("(pointer: coarse)");
  const [code, setCode] = useState("");
  const enable = useMutation({
    mutationFn: () =>
      unwrap(
        api.POST("/api/v1/account/totp/enable", {
          body: { setup_token: setup.setup_token, code },
        }),
      ),
    onSuccess: (result) => onEnabled(result.backup_codes),
    onError: (err) => {
      setCode("");
      if (err instanceof ApiError && err.code === "setup_expired") onExpired();
    },
  });

  function submit(event: SubmitEvent) {
    event.preventDefault();
    if (code.length === 6) enable.mutate();
  }

  return (
    <Modal
      open
      onOpenChange={(open) => !open && !enable.isPending && onClose()}
      title="Подключение приложения"
      description="Две минуты: добавьте kronto в приложение и подтвердите кодом из него."
      footer={
        <>
          <Button variant="ghost" size="sm" onClick={onClose} disabled={enable.isPending}>
            Отмена
          </Button>
          <Button
            type="submit"
            form={formId}
            size="sm"
            busy={enable.isPending}
            disabled={code.length !== 6}
          >
            Включить
          </Button>
        </>
      }
    >
      <form id={formId} onSubmit={submit}>
        <ol className={styles.steps}>
          <li className={styles.step}>
            <p>Откройте приложение-аутентификатор и отсканируйте QR-код.</p>
            <div className={styles.qr}>
              <QrCode value={setup.otpauth_uri} label="QR-код для приложения-аутентификатора" />
            </div>
            {touch ? (
              <p>
                Приложение на этом телефоне? <a href={setup.otpauth_uri}>Добавить kronto в него</a>
              </p>
            ) : null}
            <p className="muted">Не сканируется? Добавьте учётную запись вручную по ключу:</p>
            <div className={styles.secret}>
              <span className="visually-hidden">Ключ: </span>
              <code>{groupSecret(setup.secret)}</code>
              <CopyButton value={setup.secret} label="Скопировать ключ" />
            </div>
          </li>
          <li className={styles.step}>
            <p>Введите 6 цифр, которые показывает приложение.</p>
            {enable.isError ? <Notice kind="error">{errorMessage(enable.error)}</Notice> : null}
            <div className={styles.code}>
              <TextField
                label="Код из приложения"
                name="code"
                inputMode="numeric"
                autoComplete="one-time-code"
                required
                maxLength={6}
                value={code}
                onChange={(e) => setCode(e.target.value.replace(/\D/g, "").slice(0, 6))}
              />
            </div>
          </li>
        </ol>
      </form>
    </Modal>
  );
}

function TotpDisableDialog({
  onClose,
  onDisabled,
}: {
  onClose: () => void;
  onDisabled: () => Promise<void>;
}) {
  const me = useMe();
  const formId = useId();
  const [password, setPassword] = useState("");
  const [code, setCode] = useState("");
  const disable = useMutation({
    mutationFn: () =>
      unwrap(
        api.POST("/api/v1/account/totp/disable", {
          body: { password, code: code.trim() },
        }),
      ),
    onSuccess: onDisabled,
    onError: () => setCode(""),
  });

  function submit(event: SubmitEvent) {
    event.preventDefault();
    disable.mutate();
  }

  return (
    <Modal
      open
      onOpenChange={(open) => !open && !disable.isPending && onClose()}
      title="Отключить приложение?"
      description="Если других надёжных способов нет, при входе снова будет код на почту, а резервные коды перестанут действовать."
      footer={
        <>
          <Button variant="ghost" size="sm" onClick={onClose} disabled={disable.isPending}>
            Отмена
          </Button>
          <Button
            type="submit"
            form={formId}
            variant="danger"
            size="sm"
            busy={disable.isPending}
            disabled={!password || code.trim().length < 6}
          >
            Отключить
          </Button>
        </>
      }
    >
      <form id={formId} className={styles.form} onSubmit={submit}>
        {disable.isError ? <Notice kind="error">{errorMessage(disable.error)}</Notice> : null}
        <input
          type="email"
          name="username"
          autoComplete="username"
          value={me.email}
          readOnly
          hidden
        />
        <TextField
          label="Пароль от учётной записи"
          type="password"
          autoComplete="current-password"
          required
          value={password}
          onChange={(e) => setPassword(e.target.value)}
          autoFocus
        />
        <div className={styles.code}>
          <TextField
            label="Код из приложения или резервный код"
            name="code"
            autoComplete="one-time-code"
            autoCapitalize="characters"
            spellCheck={false}
            required
            maxLength={12}
            value={code}
            onChange={(e) => setCode(e.target.value.toUpperCase())}
          />
        </div>
      </form>
    </Modal>
  );
}
