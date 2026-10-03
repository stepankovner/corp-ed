import { useId, type ReactNode } from "react";

import styles from "./Switch.module.css";

/** Переключатель «вкл./выкл.»: role="switch", подпись — щелчком тоже. */
export function Switch({
  checked,
  onCheckedChange,
  label,
  hint,
  disabled = false,
}: {
  checked: boolean;
  onCheckedChange: (checked: boolean) => void;
  label: ReactNode;
  hint?: ReactNode;
  disabled?: boolean;
}) {
  const id = useId();
  return (
    <div className={styles.row}>
      <span className={styles.text}>
        <label className={styles.label} htmlFor={id}>
          {label}
        </label>
        {hint ? (
          <span className={styles.hint} id={`${id}-hint`}>
            {hint}
          </span>
        ) : null}
      </span>
      <button
        id={id}
        type="button"
        role="switch"
        aria-checked={checked}
        aria-describedby={hint ? `${id}-hint` : undefined}
        disabled={disabled}
        className={styles.switch}
        onClick={() => onCheckedChange(!checked)}
      >
        <span className={styles.thumb} aria-hidden />
      </button>
    </div>
  );
}
