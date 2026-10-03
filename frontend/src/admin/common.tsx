import { Check, Copy } from "lucide-react";
import { useState, type ReactNode } from "react";

import { errorMessage } from "../api/errors";
import { Button } from "../ui/Button";
import { IconButton } from "../ui/IconButton";
import { Modal } from "../ui/Modal";
import { Notice } from "../ui/Notice";
import { useToast } from "../ui/useToast";
import styles from "./Admin.module.css";

export function CopyButton({ value, label = "Скопировать" }: { value: string; label?: string }) {
  const [copied, setCopied] = useState(false);
  const toast = useToast();
  return (
    <IconButton
      size="sm"
      label={label}
      onClick={() => {
        void navigator.clipboard
          .writeText(value)
          .then(() => {
            setCopied(true);
            toast.show("Скопировано");
            window.setTimeout(() => setCopied(false), 1600);
          })
          .catch(() =>
            toast.show("Не удалось скопировать: браузер не дал доступа к буферу обмена", {
              tone: "error",
            }),
          );
      }}
    >
      {copied ? <Check size={16} aria-hidden /> : <Copy size={16} aria-hidden />}
    </IconButton>
  );
}

/** Подтверждение необратимого действия. */
export function ConfirmDialog({
  open,
  onOpenChange,
  title,
  description,
  confirmLabel,
  danger = true,
  onConfirm,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  title: ReactNode;
  description?: ReactNode;
  confirmLabel: string;
  danger?: boolean;
  onConfirm: () => Promise<unknown>;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function confirm() {
    setBusy(true);
    setError(null);
    try {
      await onConfirm();
      onOpenChange(false);
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <Modal
      open={open}
      onOpenChange={(next) => {
        if (!next) setError(null);
        onOpenChange(next);
      }}
      title={title}
      description={description}
      footer={
        <>
          <Button variant="ghost" size="sm" onClick={() => onOpenChange(false)}>
            Отмена
          </Button>
          <Button
            variant={danger ? "danger" : "dark"}
            size="sm"
            busy={busy}
            onClick={() => void confirm()}
          >
            {confirmLabel}
          </Button>
        </>
      }
    >
      {error ? <Notice kind="error">{error}</Notice> : null}
    </Modal>
  );
}

/** Одноразовый секрет (ссылка-приглашение): показать, дать скопировать. */
export function SecretValue({
  value,
  copyLabel = "Скопировать",
  testId,
}: {
  value: string;
  copyLabel?: string;
  /** data-testid значения — по нему секрет берёт e2e. */
  testId?: string;
}) {
  return (
    <div className={styles.secret}>
      <code data-testid={testId}>{value}</code>
      <CopyButton value={value} label={copyLabel} />
    </div>
  );
}
