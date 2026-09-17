import { Navigate, useLocation } from "react-router-dom";
import type { ReactNode } from "react";

import { useAuth } from "../auth/AuthContext";
import { PageLoader } from "./PageLoader";

/**
 * Пускает дальше только с токеном.
 *
 * Пока токен из localStorage проверяется запросом /auth/me, показывается
 * загрузка: отправить на вход сразу значило бы выкидывать со страницы
 * при каждом обновлении.
 */
export function ProtectedRoute({ children }: { children: ReactNode }) {
  const { status } = useAuth();
  const location = useLocation();

  if (status === "loading") {
    return <PageLoader text="Проверяем сессию" />;
  }

  if (status === "anonymous") {
    return <Navigate to="/login" replace state={{ from: location.pathname }} />;
  }

  return <>{children}</>;
}
