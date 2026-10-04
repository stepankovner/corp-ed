import {
  useId,
  type InputHTMLAttributes,
  type ReactNode,
  type SelectHTMLAttributes,
  type TextareaHTMLAttributes,
} from "react";

import styles from "./Field.module.css";

interface FrameProps {
  id: string;
  label: ReactNode;
  hint?: ReactNode;
  error?: string | null;
  optional?: boolean;
  children: ReactNode;
}

function Frame({ id, label, hint, error, optional, children }: FrameProps) {
  return (
    <div className={styles.field}>
      <label className={styles.label} htmlFor={id}>
        {label}
        {optional ? <span className={styles.optional}> — необязательно</span> : null}
      </label>
      {children}
      {hint ? (
        <p className={styles.hint} id={`${id}-hint`}>
          {hint}
        </p>
      ) : null}
      {error ? (
        <p className={styles.error} id={`${id}-error`} role="alert">
          {error}
        </p>
      ) : null}
    </div>
  );
}

function describedBy(id: string, hint: unknown, error: unknown): string | undefined {
  const ids = [hint ? `${id}-hint` : "", error ? `${id}-error` : ""].filter(Boolean);
  return ids.length ? ids.join(" ") : undefined;
}

interface Common {
  label: ReactNode;
  hint?: ReactNode;
  error?: string | null;
  optional?: boolean;
}

export function TextField({
  label,
  hint,
  error,
  optional,
  id,
  className,
  ...rest
}: Common & InputHTMLAttributes<HTMLInputElement>) {
  const auto = useId();
  const fieldId = id ?? auto;
  return (
    <Frame id={fieldId} label={label} hint={hint} error={error} optional={optional}>
      <input
        id={fieldId}
        className={[styles.control, className].filter(Boolean).join(" ")}
        aria-invalid={error ? true : undefined}
        aria-describedby={describedBy(fieldId, hint, error)}
        {...rest}
      />
    </Frame>
  );
}

export function TextAreaField({
  label,
  hint,
  error,
  optional,
  id,
  className,
  ...rest
}: Common & TextareaHTMLAttributes<HTMLTextAreaElement>) {
  const auto = useId();
  const fieldId = id ?? auto;
  return (
    <Frame id={fieldId} label={label} hint={hint} error={error} optional={optional}>
      <textarea
        id={fieldId}
        className={[styles.control, className].filter(Boolean).join(" ")}
        aria-invalid={error ? true : undefined}
        aria-describedby={describedBy(fieldId, hint, error)}
        {...rest}
      />
    </Frame>
  );
}

export function SelectField({
  label,
  hint,
  error,
  optional,
  id,
  children,
  ...rest
}: Common & SelectHTMLAttributes<HTMLSelectElement>) {
  const auto = useId();
  const fieldId = id ?? auto;
  return (
    <Frame id={fieldId} label={label} hint={hint} error={error} optional={optional}>
      <Select
        id={fieldId}
        aria-invalid={error ? true : undefined}
        aria-describedby={describedBy(fieldId, hint, error)}
        {...rest}
      >
        {children}
      </Select>
    </Frame>
  );
}

/** Список выбора без подписи-рамки: подпись даёт вызывающий (label или aria-label). */
export function Select({
  compact = false,
  className,
  ...rest
}: { compact?: boolean } & SelectHTMLAttributes<HTMLSelectElement>) {
  return (
    <span className={[styles.select, compact ? styles.compact : "", className].join(" ")}>
      <select className={styles.control} {...rest} />
    </span>
  );
}

export function Checkbox({
  label,
  ...rest
}: { label: ReactNode } & InputHTMLAttributes<HTMLInputElement>) {
  return (
    <label className={styles.check}>
      <input type="checkbox" {...rest} />
      <span>{label}</span>
    </label>
  );
}
