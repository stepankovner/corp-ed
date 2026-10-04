import type { ReactNode } from "react";
import { Navigate, Outlet, useLocation, useMatches, useSearchParams } from "react-router";

import { PageSpinner } from "../ui/Spinner";
import { isAdmin, needsStrongFactor, useAuth, useMe } from "./context";
import { safeNext } from "./next";

/**
 * handle маршрута приложения. guest — что показать гостю вместо входа:
 * на главной — сайт о продукте (ТЗ §1), на «Помощи» — статьи без формы
 * обращения, на неизвестном адресе — 404 сайта.
 */
export interface RouteHandle {
  guest?: ReactNode;
}

function guestPage(matches: ReturnType<typeof useMatches>): ReactNode {
  for (const match of [...matches].reverse()) {
    const handle = match.handle as RouteHandle | undefined;
    if (handle?.guest) return handle.guest;
  }
  return null;
}

/** Вход обязателен; временный пароль пускает только на его смену. */
export function RequireAuth() {
  const { state } = useAuth();
  const location = useLocation();
  const matches = useMatches();
  if (state.status === "loading") return <PageSpinner />;
  if (state.status === "anonymous") {
    const page = guestPage(matches);
    if (page) return page;
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

/**
 * Разделы компании. Без компании или без обязательной защиты входа
 * главная сама покажет, что делать (HomePage): вступить в компанию или
 * включить приложение-аутентификатор.
 */
export function RequireCompany() {
  const me = useMe();
  if (!me.company || needsStrongFactor(me)) return <Navigate to="/" replace />;
  return <Outlet />;
}

export function RequireAdmin() {
  const me = useMe();
  if (!isAdmin(me)) return <Navigate to="/" replace />;
  return <Outlet />;
}

/**
 * Наша панель (ТЗ §9): только команда kronto. Без приложения или ключа
 * доступа сервер панель не отдаст — сначала защита входа.
 */
export function RequireStaff() {
  const me = useMe();
  if (!me.staff) return <Navigate to="/" replace />;
  if (!me.mfa.strong) return <Navigate to="/settings/security" replace />;
  return <Outlet />;
}

/** Вход, регистрация, восстановление: вошедшего отправляем дальше. */
export function PublicOnly() {
  const { state } = useAuth();
  const [params] = useSearchParams();
  if (state.status === "loading") return <PageSpinner />;
  if (state.status === "authenticated")
    return <Navigate to={safeNext(params.get("next"))} replace />;
  return <Outlet />;
}
