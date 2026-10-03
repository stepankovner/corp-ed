import styles from "./SegmentedControl.module.css";

export interface SegmentOption<T extends string> {
  value: T;
  label: string;
  count?: number;
}

/** Переключатель из нескольких вариантов (фильтр списка): кнопки с aria-pressed. */
export function SegmentedControl<T extends string>({
  label,
  value,
  options,
  onChange,
}: {
  label: string;
  value: T;
  options: SegmentOption<T>[];
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
          {/* Пробел — чтобы скринридер читал «Все 4», а не «Все4». */}
          {option.count !== undefined ? (
            <>
              {" "}
              <span className={styles.count}>{option.count}</span>
            </>
          ) : null}
        </button>
      ))}
    </div>
  );
}
