import { useId, useRef, useState, type KeyboardEvent, type ReactNode } from "react";

import styles from "./Tabs.module.css";

export interface TabItem<T extends string> {
  value: T;
  label: ReactNode;
  content: ReactNode;
}

/**
 * Вкладки по шаблону WAI-ARIA: одна вкладка в порядке табуляции, стрелки,
 * Home и End переключают сразу. Состояние — своё или снаружи (value).
 */
export function Tabs<T extends string>({
  label,
  items,
  value,
  defaultValue,
  onValueChange,
}: {
  label: string;
  items: TabItem<T>[];
  value?: T;
  defaultValue?: T;
  onValueChange?: (value: T) => void;
}) {
  const id = useId();
  const [own, setOwn] = useState<T | undefined>(defaultValue ?? items[0]?.value);
  const active = value ?? own;
  const refs = useRef(new Map<T, HTMLButtonElement>());

  function select(next: T) {
    setOwn(next);
    onValueChange?.(next);
  }

  function onKeyDown(event: KeyboardEvent<HTMLDivElement>) {
    const index = items.findIndex((item) => item.value === active);
    const last = items.length - 1;
    const target =
      event.key === "ArrowRight"
        ? index === last
          ? 0
          : index + 1
        : event.key === "ArrowLeft"
          ? index === 0
            ? last
            : index - 1
          : event.key === "Home"
            ? 0
            : event.key === "End"
              ? last
              : null;
    const item = target === null ? undefined : items[target];
    if (!item) return;
    event.preventDefault();
    select(item.value);
    refs.current.get(item.value)?.focus();
  }

  const current = items.find((item) => item.value === active);
  return (
    <div className={styles.tabs}>
      <div className={styles.list} role="tablist" aria-label={label} onKeyDown={onKeyDown}>
        {items.map((item) => {
          const selected = item.value === active;
          return (
            <button
              key={item.value}
              ref={(node) => {
                if (node) refs.current.set(item.value, node);
                else refs.current.delete(item.value);
              }}
              type="button"
              role="tab"
              id={`${id}-tab-${item.value}`}
              aria-selected={selected}
              aria-controls={`${id}-panel`}
              tabIndex={selected ? 0 : -1}
              className={styles.tab}
              onClick={() => select(item.value)}
            >
              {item.label}
            </button>
          );
        })}
      </div>
      {current ? (
        <div
          className={styles.panel}
          role="tabpanel"
          id={`${id}-panel`}
          aria-labelledby={`${id}-tab-${current.value}`}
          tabIndex={0}
        >
          {current.content}
        </div>
      ) : null}
    </div>
  );
}
