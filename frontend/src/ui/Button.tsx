import type { ButtonHTMLAttributes } from "react";

import { buttonClass, type ButtonSize, type ButtonVariant } from "./buttonClass";
import { Spinner } from "./Spinner";

interface Props extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: ButtonVariant;
  size?: ButtonSize;
  block?: boolean;
  busy?: boolean;
}

export function Button({
  variant = "dark",
  size = "md",
  block = false,
  busy = false,
  className,
  children,
  disabled,
  type = "button",
  ...rest
}: Props) {
  return (
    <button
      type={type}
      className={[buttonClass(variant, size, block), className].filter(Boolean).join(" ")}
      disabled={disabled || busy}
      aria-busy={busy || undefined}
      {...rest}
    >
      {busy ? <Spinner size={16} /> : null}
      {children}
    </button>
  );
}
