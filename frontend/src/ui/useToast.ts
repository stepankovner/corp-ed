import { createContext, useContext, type ReactNode } from "react";

export type ToastTone = "success" | "error" | "info";

export interface ToastApi {
  /** Короткое сообщение на несколько секунд; скринридер прочитает его вежливо. */
  show: (message: ReactNode, options?: { tone?: ToastTone }) => void;
}

export const ToastContext = createContext<ToastApi | null>(null);

export function useToast(): ToastApi {
  const value = useContext(ToastContext);
  if (!value) throw new Error("useToast outside ToastProvider");
  return value;
}
