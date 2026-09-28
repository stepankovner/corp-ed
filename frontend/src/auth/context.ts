import { createContext, useContext } from "react";

import type { Schemas } from "../api/client";

export type Me = Schemas["MeResponse"];

export type AuthState =
  { status: "loading" } | { status: "anonymous" } | { status: "authenticated"; user: Me };

export interface AuthApi {
  state: AuthState;
  login: (company: string, email: string, password: string) => Promise<Me>;
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

const COMPANY_KEY = "kronto.company";

export function rememberedCompany(): string {
  try {
    return localStorage.getItem(COMPANY_KEY) ?? "";
  } catch {
    return "";
  }
}

export function rememberCompany(code: string): void {
  try {
    localStorage.setItem(COMPANY_KEY, code);
  } catch {
    // Не запомнили — введёт ещё раз.
  }
}
