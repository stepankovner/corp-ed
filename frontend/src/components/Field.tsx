import { useId } from "react";
import type {
  InputHTMLAttributes,
  SelectHTMLAttributes,
  TextareaHTMLAttributes,
} from "react";

import styles from "./Field.module.css";

interface Shared {
  label: string;
  /** Сообщение валидации с бэкенда или из формы — показывается под полем. */
  error?: string;
  hint?: string;
}

function Wrapper({
  label,
  error,
  hint,
  id,
  children,
}: Shared & { id: string; children: React.ReactNode }) {
  return (
    <div className={styles.field}>
      <label className={styles.label} htmlFor={id}>
        {label}
      </label>
      {children}
      {hint && !error ? <span className={styles.hint}>{hint}</span> : null}
      {error ? <span className={styles.error}>{error}</span> : null}
    </div>
  );
}

type InputProps = Shared & InputHTMLAttributes<HTMLInputElement>;

export function TextField({ label, error, hint, ...rest }: InputProps) {
  const id = useId();
  return (
    <Wrapper label={label} error={error} hint={hint} id={id}>
      <input
        id={id}
        className={`${styles.control} ${error ? styles.invalid : ""}`}
        aria-invalid={error ? true : undefined}
        {...rest}
      />
    </Wrapper>
  );
}

type AreaProps = Shared & TextareaHTMLAttributes<HTMLTextAreaElement>;

export function TextArea({ label, error, hint, ...rest }: AreaProps) {
  const id = useId();
  return (
    <Wrapper label={label} error={error} hint={hint} id={id}>
      <textarea
        id={id}
        className={`${styles.control} ${error ? styles.invalid : ""}`}
        aria-invalid={error ? true : undefined}
        {...rest}
      />
    </Wrapper>
  );
}

type SelectProps = Shared & SelectHTMLAttributes<HTMLSelectElement>;

export function SelectField({
  label,
  error,
  hint,
  children,
  ...rest
}: SelectProps) {
  const id = useId();
  return (
    <Wrapper label={label} error={error} hint={hint} id={id}>
      <select
        id={id}
        className={`${styles.control} ${error ? styles.invalid : ""}`}
        aria-invalid={error ? true : undefined}
        {...rest}
      >
        {children}
      </select>
    </Wrapper>
  );
}
