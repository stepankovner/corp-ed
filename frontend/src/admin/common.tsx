import { Check, Copy } from "lucide-react";
import { useState, type ReactNode } from "react";

import { errorMessage } from "../api/errors";
import { Button } from "../ui/Button";
import { IconButton } from "../ui/IconButton";
import { Modal } from "../ui/Modal";
import { Notice } from "../ui/Notice";
import styles from "./Admin.module.css";

export function Segmented<T extends string>({
  label,
  value,
  options,
  onChange,
}: {
  label: string;
  value: T;
  options: { value: T; label: string; count?: number }[];
  onChange: (value: T) => void;
}) {
  return (
    <div className={styles.segmented} role="group" aria-label={label}>
      {options.map((option) => (
        <button
          key={option.value}
          type="button"
          className={styles.segment}
          aria-pressed={option.value === value}
          onClick={() => onChange(option.value)}
        >
          {option.label}
          {option.count !== undefined ? <span className={styles.count}>{option.count}</span> : null}
        </button>
      ))}
    </div>
  );
}

export function CopyButton({ value, label = "Скопировать" }: { value: string; label?: string }) {
  const [copied, setCopied] = useState(false);
  return (
    <IconButton
      size="sm"
      label={copied ? "Скопировано" : label}
      onClick={() => {
        void navigator.clipboard
          .writeText(value)
          .then(() => {
            setCopied(true);
            window.setTimeout(() => setCopied(false), 1600);
          })
          .catch(() => undefined);
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

/** Одноразовый секрет (временный пароль): показать, дать скопировать. */
export function SecretValue({ value }: { value: string }) {
  return (
    <div className={styles.secret}>
      <code>{value}</code>
      <CopyButton value={value} label="Скопировать пароль" />
    </div>
  );
}
