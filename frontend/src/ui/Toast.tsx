import { CircleAlert, CircleCheck, Info, X } from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react";

import styles from "./Toast.module.css";
import { ToastContext, type ToastApi, type ToastTone } from "./useToast";

interface Item {
  id: number;
  message: ReactNode;
  tone: ToastTone;
}

const ICONS = { success: CircleCheck, error: CircleAlert, info: Info };
const DURATION = 4000;
const MAX_VISIBLE = 3;

/**
 * Всплывающие уведомления. Область aria-live есть в документе всегда —
 * иначе скринридер не заметит первое сообщение.
 */
export function ToastProvider({ children }: { children: ReactNode }) {
  const [items, setItems] = useState<Item[]>([]);
  const counter = useRef(0);
  const timers = useRef(new Map<number, number>());

  const dismiss = useCallback((id: number) => {
    window.clearTimeout(timers.current.get(id));
    timers.current.delete(id);
    setItems((list) => list.filter((item) => item.id !== id));
  }, []);

  const show = useCallback<ToastApi["show"]>(
    (message, options) => {
      counter.current += 1;
      const id = counter.current;
      setItems((list) => [
        ...list.slice(-(MAX_VISIBLE - 1)),
        { id, message, tone: options?.tone ?? "success" },
      ]);
      timers.current.set(
        id,
        window.setTimeout(() => dismiss(id), DURATION),
      );
    },
    [dismiss],
  );

  useEffect(() => {
    const pending = timers.current;
    return () => pending.forEach((timer) => window.clearTimeout(timer));
  }, []);

  const api = useMemo(() => ({ show }), [show]);

  return (
    <ToastContext.Provider value={api}>
      {children}
      <div className={styles.region} role="status" aria-live="polite">
        {items.map((item) => {
          const Icon = ICONS[item.tone];
          return (
            <div key={item.id} className={`${styles.toast} ${styles[item.tone]}`}>
              <Icon size={18} aria-hidden className={styles.icon} />
              <span className={styles.message}>{item.message}</span>
              <button
                type="button"
                className={styles.close}
                aria-label="Закрыть уведомление"
                onClick={() => dismiss(item.id)}
              >
                <X size={16} aria-hidden />
              </button>
            </div>
          );
        })}
      </div>
    </ToastContext.Provider>
  );
}
