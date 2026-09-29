import { useQuery, useQueryClient } from "@tanstack/react-query";
import {
  useCallback,
  useEffect,
  useMemo,
  useState,
  useSyncExternalStore,
  type ReactNode,
} from "react";

import { api, restoreSession, unwrap } from "../api/client";
import {
  announce,
  dropSession,
  getSession,
  hasSignedInBefore,
  onAnnouncement,
  sessionFromTokens,
  setSession,
  subscribeSession,
} from "../api/session";
import { clearChatHistory } from "../chat/store";
import { AuthContext, rememberCompany, type AuthApi, type AuthState, type Me } from "./context";

const ME = ["me"] as const;

async function fetchMe(): Promise<Me> {
  return unwrap(api.GET("/api/v1/auth/me"));
}

/**
 * Состояние входа выводится из access-токена в памяти вкладки и профиля
 * /auth/me в кэше запросов. Своё у провайдера только «восстанавливаю
 * сессию»: после перезагрузки токена в памяти нет, и пока refresh по
 * cookie не ответил, показывать форму входа рано.
 */
export function AuthProvider({ children }: { children: ReactNode }) {
  const queryClient = useQueryClient();
  const session = useSyncExternalStore(subscribeSession, getSession, () => null);
  const [restoring, setRestoring] = useState(() => getSession() === null && hasSignedInBefore());

  useEffect(() => {
    if (!restoring) return;
    let active = true;
    // Сеть недоступна — показываем вход; признак входа остаётся, и
    // следующая загрузка страницы попробует снова.
    void restoreSession()
      .catch(() => false)
      .finally(() => {
        if (active) setRestoring(false);
      });
    return () => {
      active = false;
    };
  }, [restoring]);

  useEffect(
    () =>
      onAnnouncement((event) => {
        // Другая вкладка вошла (возможно, другим человеком) или вышла:
        // здесь не должно остаться ни чужого токена, ни данных.
        dropSession();
        if (event === "signed-in") setRestoring(true);
      }),
    [],
  );
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
    if (!hasSession) return restoring ? { status: "loading" } : { status: "anonymous" };
    if (me.data) return { status: "authenticated", user: me.data };
    // Сервер недоступен или профиль не отдан — показываем вход: новый вход
    // заменит пару токенов.
    if (me.isError) return { status: "anonymous" };
    return { status: "loading" };
  }, [hasSession, restoring, me.data, me.isError]);

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
      announce("signed-in");
      return reloadMe();
    },
    [queryClient, reloadMe],
  );

  const logout = useCallback(async () => {
    if (getSession()) {
      // Сервер отзывает refresh и стирает cookie; если сеть упала — всё
      // равно выходим.
      await api.POST("/api/v1/auth/logout").catch(() => undefined);
    }
    setSession(null);
    announce("signed-out");
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

  // Присоединение по ссылке-приглашению: учётка создаётся и сразу входит.
  const acceptInvite = useCallback<AuthApi["acceptInvite"]>(
    async ({ company, token, email, fullName, password }) => {
      const tokens = await unwrap(
        api.POST("/api/v1/invites/accept", {
          body: {
            company_code: company,
            token,
            email,
            full_name: fullName || null,
            password,
          },
        }),
      );
      rememberCompany(company);
      queryClient.clear();
      setSession(sessionFromTokens(tokens));
      announce("signed-in");
      return reloadMe();
    },
    [queryClient, reloadMe],
  );

  const value = useMemo<AuthApi>(
    () => ({ state, login, logout, changePassword, acceptInvite }),
    [state, login, logout, changePassword, acceptInvite],
  );
  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}
