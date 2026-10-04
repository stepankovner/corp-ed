import { useEffect, useState } from "react";

import authStyles from "./AuthLayout.module.css";

const COOLDOWN_S = 60;

/**
 * «Отправить ещё раз» с паузой в минуту: письмо идёт не мгновенно, а
 * сервер всё равно ограничивает частоту. Пауза идёт и сразу после
 * открытия — письмо только что отправлено.
 */
export function ResendLink({
  onResend,
  label = "Отправить письмо ещё раз",
}: {
  /** Ошибку показывает вызывающий и пробрасывает дальше: «отправили» не пишем. */
  onResend: () => Promise<unknown>;
  label?: string;
}) {
  const [left, setLeft] = useState(COOLDOWN_S);
  const [sent, setSent] = useState(false);

  useEffect(() => {
    if (left <= 0) return;
    const timer = window.setTimeout(() => setLeft((value) => value - 1), 1000);
    return () => window.clearTimeout(timer);
  }, [left]);

  async function resend() {
    setLeft(COOLDOWN_S);
    setSent(false);
    try {
      await onResend();
      setSent(true);
    } catch {
      // Текст ошибки уже на странице.
    }
  }

  return (
    <p className={`muted ${authStyles.alt}`} aria-live="polite">
      {sent ? "Отправили ещё раз. " : null}
      {left > 0 ? (
        `${label} можно через ${left} с`
      ) : (
        <button type="button" className={authStyles.linkButton} onClick={() => void resend()}>
          {label}
        </button>
      )}
    </p>
  );
}
