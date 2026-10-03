import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useState, type SubmitEvent } from "react";

import { api, unwrap } from "../api/client";
import { errorMessage } from "../api/errors";
import { needsStrongFactor, useAuth, useMe } from "../auth/context";
import { MIN_PASSWORD, passwordProblem } from "../auth/password";
import { useDocumentTitle } from "../lib/title";
import { Badge } from "../ui/Badge";
import { Button } from "../ui/Button";
import { TextField } from "../ui/Field";
import { Notice } from "../ui/Notice";
import { SkeletonList } from "../ui/Skeleton";
import { useToast } from "../ui/useToast";
import { BackupCodesDialog } from "./BackupCodesDialog";
import { PasswordDialog, Section } from "./common";
import { SECURITY_KEY, SESSIONS_KEY } from "./keys";
import { PasskeysSection } from "./PasskeysSection";
import { SessionsSection } from "./SessionsSection";
import styles from "./Settings.module.css";
import { TotpSection } from "./TotpSection";

const ALL_BACKUP_CODES = 10;

/**
 * Пароль, второй фактор и сеансы (ТЗ §3). По умолчанию второй фактор —
 * код на почту; приложение и ключ доступа надёжнее, администраторам они
 * обязательны.
 */
export function SecurityTab() {
  useDocumentTitle("Безопасность");
  const me = useMe();
  const security = useQuery({
    queryKey: SECURITY_KEY,
    queryFn: () => unwrap(api.GET("/api/v1/account/security")),
  });
  // Коды показываются один раз — окно живёт здесь, а не в разделе: раздел
  // может пропасть, пока состояние защиты перечитывается.
  const [codes, setCodes] = useState<string[] | null>(null);
  const showCodes = (next: string[] | null | undefined) => {
    if (next?.length) setCodes(next);
  };

  return (
    <div className={styles.stack}>
      {needsStrongFactor(me) ? (
        <Notice kind="warn" title="Включите приложение-аутентификатор или ключ доступа">
          {me.company?.role === "admin"
            ? "Вы администратор компании, и вход в учётную запись нужно защитить надёжнее, чем кодом на почту. "
            : "В вашей компании вход защищают приложением или ключом доступа. "}
          Пока одно из них не включено, данные компании недоступны — это займёт пару минут.
        </Notice>
      ) : null}
      <p className={styles.lead}>
        При входе с нового устройства kronto просит подтверждение. По умолчанию — код на почту.
        Приложение-аутентификатор и ключ доступа надёжнее: когда включено одно из них, кода на почту
        для входа уже недостаточно.
      </p>
      {security.data ? (
        <>
          <TotpSection security={security.data} onCodes={showCodes} />
          <PasskeysSection security={security.data} onCodes={showCodes} />
          {security.data.totp_enabled || security.data.passkeys.length > 0 ? (
            <BackupCodesSection left={security.data.backup_codes_left} onCodes={showCodes} />
          ) : null}
        </>
      ) : security.isError ? (
        <Notice kind="error">{errorMessage(security.error)}</Notice>
      ) : (
        <SkeletonList rows={3} label="Загрузка настроек защиты" />
      )}
      <PasswordSection />
      <SessionsSection />
      {codes ? <BackupCodesDialog codes={codes} onClose={() => setCodes(null)} /> : null}
    </div>
  );
}

function PasswordSection() {
  const me = useMe();
  const { changePassword } = useAuth();
  const queryClient = useQueryClient();
  const toast = useToast();
  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const [repeat, setRepeat] = useState("");
  const [touched, setTouched] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const problem = passwordProblem(next, repeat, me.email);

  async function submit(event: SubmitEvent) {
    event.preventDefault();
    setTouched(true);
    if (problem) return;
    setError(null);
    setBusy(true);
    try {
      await changePassword(current, next);
      setCurrent("");
      setNext("");
      setRepeat("");
      setTouched(false);
      toast.show("Пароль изменён. Сеансы на других устройствах завершены.");
      await queryClient.invalidateQueries({ queryKey: SESSIONS_KEY });
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <Section
      title="Пароль"
      description="После смены пароля сеансы на других устройствах завершатся, а на почту придёт письмо об изменении."
    >
      <form className={styles.form} onSubmit={submit}>
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
          label="Текущий пароль"
          type="password"
          autoComplete="current-password"
          required
          value={current}
          onChange={(e) => setCurrent(e.target.value)}
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
        <div className={styles.actions}>
          <Button type="submit" size="sm" busy={busy}>
            Сменить пароль
          </Button>
        </div>
      </form>
    </Section>
  );
}

function BackupCodesSection({
  left,
  onCodes,
}: {
  left: number;
  onCodes: (codes: string[] | null) => void;
}) {
  const queryClient = useQueryClient();
  const [confirming, setConfirming] = useState(false);
  const low = left <= 3;

  return (
    <Section
      title="Резервные коды"
      description="На случай, если телефон или ключ доступа потеряется: каждый код срабатывает один раз. Новые коды отменяют прежние."
      aside={
        <Badge tone={left === 0 ? "error" : low ? "warn" : "muted"}>
          осталось {left} из {ALL_BACKUP_CODES}
        </Badge>
      }
    >
      {low ? (
        <Notice kind={left === 0 ? "error" : "warn"}>
          {left === 0
            ? "Коды закончились. Выпустите новые и сохраните их в надёжном месте."
            : "Кодов почти не осталось — выпустите новые."}
        </Notice>
      ) : null}
      <div className={styles.actions}>
        <Button variant="ghost" size="sm" onClick={() => setConfirming(true)}>
          Выпустить новые коды
        </Button>
      </div>
      {confirming ? (
        <PasswordDialog
          title="Выпустить новые коды?"
          description="Прежние резервные коды перестанут действовать."
          confirmLabel="Выпустить"
          onClose={() => setConfirming(false)}
          onConfirm={async (password) => {
            const result = await unwrap(
              api.POST("/api/v1/account/backup-codes", { body: { password } }),
            );
            setConfirming(false);
            onCodes(result.backup_codes);
            await queryClient.invalidateQueries({ queryKey: SECURITY_KEY });
          }}
        />
      ) : null}
    </Section>
  );
}
