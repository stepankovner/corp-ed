import { Navigate, Outlet, useLocation, useSearchParams } from "react-router";

import { PageSpinner } from "../ui/Spinner";
import { useAuth } from "./context";
import { safeNext } from "./next";

/** Вход обязателен; временный пароль пускает только на его смену. */
export function RequireAuth() {
  const { state } = useAuth();
  const location = useLocation();
  if (state.status === "loading") return <PageSpinner />;
  if (state.status === "anonymous") {
    const next = location.pathname + location.search;
    return (
      <Navigate to={`/login${next === "/" ? "" : `?next=${encodeURIComponent(next)}`}`} replace />
    );
  }
  if (state.user.must_change_password && location.pathname !== "/change-password") {
    return <Navigate to="/change-password" replace />;
  }
  return <Outlet />;
}

export function RequireAdmin() {
  const { state } = useAuth();
  if (state.status !== "authenticated") return null;
  if (state.user.role !== "admin") return <Navigate to="/" replace />;
  return <Outlet />;
}

/** Страница входа: вошедшего отправляем дальше. */
export function PublicOnly() {
  const { state } = useAuth();
  const [params] = useSearchParams();
  if (state.status === "loading") return <PageSpinner />;
  if (state.status === "authenticated")
    return <Navigate to={safeNext(params.get("next"))} replace />;
  return <Outlet />;
}
