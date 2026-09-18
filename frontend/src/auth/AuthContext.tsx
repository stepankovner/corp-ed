import {
  createContext,
  use,
  useCallback,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from "react";

import { setUnauthorizedHandler } from "../api/client";
import { fetchMe, login as loginRequest } from "../api/endpoints";
import type { Me } from "../api/types";

/**
 * Токен лежит в localStorage, чтобы обновление страницы не выбрасывало
 * из интерфейса.
 *
 * Для продакшена это неприемлемо: любая XSS-уязвимость читает
 * localStorage и уносит токен. Боевой вариант — httpOnly-кука
 * с SameSite и CSRF-защитой, и его придётся пересматривать вместе
 * с бэкендом, который сейчас ждёт заголовок Authorization.
 */
const TOKEN_KEY = "corp-ed.token";

type Status = "loading" | "anonymous" | "authenticated";

interface AuthValue {
  status: Status;
  token: string | null;
  user: Me | null;
  login: (
    companyCode: string,
    email: string,
    password: string,
  ) => Promise<Me>;
  logout: () => void;
}

const AuthContext = createContext<AuthValue | null>(null);

function readToken(): string | null {
  try {
    return localStorage.getItem(TOKEN_KEY);
  } catch {
    return null;
  }
}

export function AuthProvider({ children }: { children: ReactNode }) {
  const [token, setToken] = useState<string | null>(readToken);
  const [user, setUser] = useState<Me | null>(null);
  const [status, setStatus] = useState<Status>(token ? "loading" : "anonymous");

  const logout = useCallback(() => {
    localStorage.removeItem(TOKEN_KEY);
    setToken(null);
    setUser(null);
    setStatus("anonymous");
  }, []);

  // Любой 401 из любого запроса заканчивается разлогиниванием:
  // протухший токен не должен оставлять интерфейс наполовину живым.
  useEffect(() => {
    setUnauthorizedHandler(logout);
  }, [logout]);

  // Токен пережил перезагрузку — надо понять, чей он и жив ли ещё.
  useEffect(() => {
    if (!token || user) {
      return;
    }
    let cancelled = false;

    fetchMe(token)
      .then((me) => {
        if (!cancelled) {
          setUser(me);
          setStatus("authenticated");
        }
      })
      .catch(() => {
        if (!cancelled) {
          logout();
        }
      });

    return () => {
      cancelled = true;
    };
  }, [token, user, logout]);

  const login = useCallback(
    async (companyCode: string, email: string, password: string) => {
      const { access_token: accessToken } = await loginRequest(
        companyCode,
        email,
        password,
      );
      const me = await fetchMe(accessToken);

      localStorage.setItem(TOKEN_KEY, accessToken);
      setToken(accessToken);
      setUser(me);
      setStatus("authenticated");

      return me;
    },
    [],
  );

  const value = useMemo<AuthValue>(
    () => ({ status, token, user, login, logout }),
    [status, token, user, login, logout],
  );

  return <AuthContext value={value}>{children}</AuthContext>;
}

export function useAuth(): AuthValue {
  const value = use(AuthContext);
  if (value === null) {
    throw new Error("useAuth вызван вне AuthProvider");
  }
  return value;
}

/** Токен защищённого экрана: там он заведомо есть. */
export function useToken(): string {
  const { token } = useAuth();
  if (token === null) {
    throw new Error("Экран открыт без токена");
  }
  return token;
}
