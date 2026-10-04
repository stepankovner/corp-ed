import type { ReactNode } from "react";

import { ToastProvider } from "./Toast";
import { TooltipProvider } from "./Tooltip";

/** Общие для всех экранов подсказки и уведомления; в тестах — те же. */
export function UiProvider({ children }: { children: ReactNode }) {
  return (
    <TooltipProvider>
      <ToastProvider>{children}</ToastProvider>
    </TooltipProvider>
  );
}
