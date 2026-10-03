import { useId, useState, type ReactNode, type SubmitEvent } from "react";

import { errorMessage } from "../api/errors";
import { useMe } from "../auth/context";
import { Button } from "../ui/Button";
import { TextField } from "../ui/Field";
import { Modal } from "../ui/Modal";
import { Notice } from "../ui/Notice";
import styles from "./Settings.module.css";

/** Раздел вкладки: карточка с заголовком; для скринридера — область с именем. */
export function Section({
  title,
  description,
  aside,
  danger = false,
  children,
}: {
  title: ReactNode;
  description?: ReactNode;
  /** Справа от заголовка: состояние или главное действие. */
  aside?: ReactNode;
  danger?: boolean;
  children?: ReactNode;
}) {
  const id = useId();
  return (
    <section
      className={[styles.section, danger ? styles.danger : ""].join(" ")}
      aria-labelledby={id}
    >
      <div className={styles.sectionHead}>
        <div>
          <h2 id={id} className={styles.sectionTitle}>
            {title}
          </h2>
          {description ? <p className={styles.sectionText}>{description}</p> : null}
        </div>
        {aside}
      </div>
      {children}
    </section>
  );
}

/**
 * Действие, которое сервер подтверждает паролем: удалить ключ, выпустить
 * коды, удалить учётку. Окно монтируется на время действия — поля и
 * ошибка не переживают закрытие.
 */
export function PasswordDialog({
  title,
  description,
  confirmLabel,
  danger = false,
  children,
  onClose,
  onConfirm,
}: {
  title: ReactNode;
  description?: ReactNode;
  confirmLabel: string;
  danger?: boolean;
  /** Над полем пароля: последствия действия. */
  children?: ReactNode;
  onClose: () => void;
  onConfirm: (password: string) => Promise<unknown>;
}) {
  const me = useMe();
  const formId = useId();
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function submit(event: SubmitEvent) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await onConfirm(password);
    } catch (err) {
      setError(errorMessage(err));
      setPassword("");
      setBusy(false);
    }
  }

  return (
    <Modal
      open
      onOpenChange={(open) => !open && !busy && onClose()}
      title={title}
      description={description}
      footer={
        <>
          <Button variant="ghost" size="sm" onClick={onClose} disabled={busy}>
            Отмена
          </Button>
          <Button
            type="submit"
            form={formId}
            variant={danger ? "danger" : "dark"}
            size="sm"
            busy={busy}
            disabled={!password}
          >
            {confirmLabel}
          </Button>
        </>
      }
    >
      <form id={formId} className={styles.form} onSubmit={submit}>
        {children}
        {error ? <Notice kind="error">{error}</Notice> : null}
        {/* Менеджер паролей подставит пароль этой учётки. */}
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
      </form>
    </Modal>
  );
}
