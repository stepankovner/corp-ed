import styles from "./Segmented.module.css";

interface Option<T extends string> {
  value: T;
  label: string;
}

interface Props<T extends string> {
  /** Подпись над переключателем, в том же стиле, что у полей формы. */
  label: string;
  value: T;
  options: Option<T>[];
  onChange: (value: T) => void;
}

/** Выбор из двух-трёх вариантов: короче выпадающего списка и виден целиком. */
export function Segmented<T extends string>({
  label,
  value,
  options,
  onChange,
}: Props<T>) {
  return (
    <div className="stack gap-6">
      <span className="label">{label}</span>
      <div className={styles.group} role="group" aria-label={label}>
        {options.map((option) => (
          <button
            key={option.value}
            type="button"
            className={`${styles.option} ${
              option.value === value ? styles.selected : ""
            }`}
            aria-pressed={option.value === value}
            onClick={() => onChange(option.value)}
          >
            {option.label}
          </button>
        ))}
      </div>
    </div>
  );
}
