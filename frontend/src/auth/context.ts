import { createContext, useContext } from "react";

import type { Schemas } from "../api/client";

export type Me = Schemas["MeResponse"];
/** Выбранная компания учётки. */
export type Company = NonNullable<Me["company"]>;
export type MfaChallenge = Schemas["MfaChallenge"];
export type MfaMethod = MfaChallenge["methods"][number];
export type Tokens = Schemas["TokenResponse"];

export type AuthState =
  { status: "loading" } | { status: "anonymous" } | { status: "authenticated"; user: Me };

/** Пароль верный: либо сразу вход (доверенное устройство), либо второй шаг. */
export type LoginOutcome =
  { status: "signed-in"; me: Me } | { status: "mfa"; challenge: MfaChallenge };

export interface SecondFactor {
  token: string;
  method: MfaMethod;
  code?: string;
  credential?: Record<string, unknown>;
}

export interface AuthApi {
  state: AuthState;
  /** Первый шаг входа: почта и пароль. remember — доверить устройство на 30 дней. */
  login: (email: string, password: string, remember: boolean) => Promise<LoginOutcome>;
  /** Второй шаг: код из письма или приложения, резервный код, ключ доступа. */
  verifySecondFactor: (factor: SecondFactor) => Promise<Me>;
  /**
   * Сессия, которую выдала другая ручка: подтверждение почты, новый
   * пароль, вступление в компанию.
   */
  signIn: (tokens: Tokens) => Promise<Me>;
  /** Перейти в другую свою компанию; null — без компании. */
  switchCompany: (tenantId: string | null) => Promise<Me>;
  /** Перечитать профиль после изменений учётки (имя, защита, компании). */
  reloadMe: () => Promise<Me>;
  logout: () => Promise<void>;
  changePassword: (current: string, next: string) => Promise<Me>;
}

export const AuthContext = createContext<AuthApi | null>(null);

export function useAuth(): AuthApi {
  const value = useContext(AuthContext);
  if (!value) throw new Error("useAuth outside AuthProvider");
  return value;
}

/** Текущий пользователь; только внутри защищённых маршрутов. */
export function useMe(): Me {
  const { state } = useAuth();
  if (state.status !== "authenticated") throw new Error("useMe outside RequireAuth");
  return state.user;
}

/** Выбранная компания; только внутри маршрутов компании (RequireCompany). */
export function useCompany(): Company {
  const { company } = useMe();
  if (!company) throw new Error("useCompany outside RequireCompany");
  return company;
}

export function isAdmin(me: Me): boolean {
  return me.company?.role === "admin";
}

/**
 * Надёжный второй фактор обязателен (администратор хоть в одной компании
 * или правило компании), а его нет: данные компании сервер не отдаст
 * (403 mfa_setup_required) — сначала настройка защиты.
 */
export function needsStrongFactor(me: Me): boolean {
  return me.mfa.strong_required && !me.mfa.strong;
}

export function displayName(me: Pick<Me, "full_name" | "email">): string {
  return me.full_name || me.email;
}
