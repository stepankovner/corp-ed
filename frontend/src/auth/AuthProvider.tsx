import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useCallback, useEffect, useMemo, useSyncExternalStore, type ReactNode } from "react";

import { api, unwrap } from "../api/client";
import { getSession, sessionFromTokens, setSession, subscribeSession } from "../api/session";
import { clearChatHistory } from "../chat/store";
import { AuthContext, rememberCompany, type AuthApi, type AuthState, type Me } from "./context";

const ME = ["me"] as const;

async function fetchMe(): Promise<Me> {
  return unwrap(api.GET("/api/v1/auth/me"));
}

/**
 * Состояние входа выводится из двух источников: пары токенов в хранилище
 * (общей для вкладок) и профиля /auth/me в кэше запросов. Своего
 * состояния у провайдера нет — нечему рассинхронизироваться.
 */
export function AuthProvider({ children }: { children: ReactNode }) {
  const queryClient = useQueryClient();
  const session = useSyncExternalStore(subscribeSession, getSession, () => null);
  const me = useQuery({
    queryKey: ME,
    queryFn: fetchMe,
    enabled: session !== null,
    retry: false,
    staleTime: Infinity,
  });

  useEffect(
    () =>
      subscribeSession((next) => {
        if (next) return;
        // Выход (здесь, в соседней вкладке или отказ в обновлении токена):
        // в браузере не должно остаться ни данных, ни переписки.
        queryClient.clear();
        clearChatHistory();
      }),
    [queryClient],
  );

  const hasSession = session !== null;
  const state = useMemo<AuthState>(() => {
    if (!hasSession) return { status: "anonymous" };
    if (me.data) return { status: "authenticated", user: me.data };
    // Сервер недоступен или профиль не отдан — показываем вход: новый вход
    // заменит пару токенов.
    if (me.isError) return { status: "anonymous" };
    return { status: "loading" };
  }, [hasSession, me.data, me.isError]);

  const reloadMe = useCallback(
    () => queryClient.query({ queryKey: ME, queryFn: fetchMe, staleTime: 0 }),
    [queryClient],
  );

  const login = useCallback<AuthApi["login"]>(
    async (company, email, password) => {
      const tokens = await unwrap(
        api.POST("/api/v1/auth/login", { body: { company_code: company, email, password } }),
      );
      rememberCompany(company);
      queryClient.clear();
      setSession(sessionFromTokens(tokens));
      return reloadMe();
    },
    [queryClient, reloadMe],
  );

  const logout = useCallback(async () => {
    const current = getSession();
    if (current) {
      // Отзываем refresh на сервере; если сеть упала — всё равно выходим.
      await api
        .POST("/api/v1/auth/logout", { body: { refresh_token: current.refreshToken } })
        .catch(() => undefined);
    }
    setSession(null);
  }, []);

  const changePassword = useCallback<AuthApi["changePassword"]>(
    async (current, next) => {
      const tokens = await unwrap(
        api.POST("/api/v1/auth/change-password", {
          body: { current_password: current, new_password: next },
        }),
      );
      setSession(sessionFromTokens(tokens));
      return reloadMe();
    },
    [reloadMe],
  );

  const value = useMemo<AuthApi>(
    () => ({ state, login, logout, changePassword }),
    [state, login, logout, changePassword],
  );
  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}
