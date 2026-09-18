import { useId } from "react";
import type {
  InputHTMLAttributes,
  ReactNode,
  SelectHTMLAttributes,
  TextareaHTMLAttributes,
} from "react";

import styles from "./Field.module.css";

interface Shared {
  label: string;
  /** Сообщение валидации с бэкенда или из формы — показывается под полем. */
  error?: string;
  hint?: string;
  /** Правый край строки с подписью: счётчик символов и подобное. */
  aside?: ReactNode;
}

function Wrapper({
  label,
  error,
  hint,
  aside,
  id,
  children,
}: Shared & { id: string; children: ReactNode }) {
  return (
    <div className={styles.field}>
      <div className={styles.head}>
        <label className="label" htmlFor={id}>
          {label}
        </label>
        {aside}
      </div>
      {children}
      {hint && !error ? <span className={styles.hint}>{hint}</span> : null}
      {error ? <span className={styles.error}>{error}</span> : null}
    </div>
  );
}

type InputProps = Shared & InputHTMLAttributes<HTMLInputElement>;

export function TextField({ label, error, hint, aside, ...rest }: InputProps) {
  const id = useId();
  return (
    <Wrapper label={label} error={error} hint={hint} aside={aside} id={id}>
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

export function TextArea({ label, error, hint, aside, ...rest }: AreaProps) {
  const id = useId();
  return (
    <Wrapper label={label} error={error} hint={hint} aside={aside} id={id}>
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
  aside,
  children,
  ...rest
}: SelectProps) {
  const id = useId();
  return (
    <Wrapper label={label} error={error} hint={hint} aside={aside} id={id}>
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
