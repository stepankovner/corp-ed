import { useQuery, useQueryClient } from "@tanstack/react-query";
import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
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
  tokenScope,
} from "../api/session";
import { clearChatHistory } from "../chat/store";
import { AuthContext, type AuthApi, type AuthState, type Me, type Tokens } from "./context";

function meKey(scope: string | null) {
  return ["me", scope] as const;
}

async function fetchMe(): Promise<Me> {
  return unwrap(api.GET("/api/v1/auth/me"));
}

/**
 * Состояние входа выводится из access-токена в памяти вкладки и профиля
 * /auth/me в кэше запросов. Своё у провайдера только «восстанавливаю
 * сессию»: после перезагрузки токена в памяти нет, и пока refresh по
 * cookie не ответил, показывать форму входа рано.
 *
 * Профиль привязан к области токена (учётка, компания, членство, роль):
 * переключение компании, исключение из неё или смена роли меняют область
 * при очередном обновлении токена — профиль и данные компании
 * запрашиваются заново, чужие данные в кэше не остаются.
 */
export function AuthProvider({ children }: { children: ReactNode }) {
  const queryClient = useQueryClient();
  const session = useSyncExternalStore(subscribeSession, getSession, () => null);
  const scope = session ? tokenScope(session.accessToken) : null;
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
        // Другая вкладка вошла (возможно, другим человеком), переключила
        // компанию или вышла: здесь не должно остаться ни чужого токена,
        // ни данных.
        dropSession();
        if (event === "signed-in") setRestoring(true);
      }),
    [],
  );
  const me = useQuery({
    queryKey: meKey(scope),
    queryFn: fetchMe,
    enabled: session !== null,
    retry: false,
    staleTime: Infinity,
  });

  // Область сменилась без нашего участия (refresh после исключения из
  // компании или смены роли): данные прежней компании — не показывать.
  // Пока профиль новой области грузится, маршруты стоят на спиннере.
  const shownScope = useRef(scope);
  useEffect(() => {
    const previous = shownScope.current;
    shownScope.current = scope;
    if (previous === null || scope === null || previous === scope) return;
    queryClient.removeQueries({ predicate: (query) => query.queryKey[0] !== "me" });
    clearChatHistory();
  }, [scope, queryClient]);

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

  const reloadMe = useCallback(() => {
    const current = getSession();
    return queryClient.query({
      queryKey: meKey(current ? tokenScope(current.accessToken) : null),
      queryFn: fetchMe,
      staleTime: 0,
    });
  }, [queryClient]);

  /** Новая сессия: другой человек или другая компания — кэш с чистого листа. */
  const signIn = useCallback(
    (tokens: Tokens) => {
      queryClient.clear();
      clearChatHistory();
      setSession(sessionFromTokens(tokens));
      announce("signed-in");
      return reloadMe();
    },
    [queryClient, reloadMe],
  );

  const login = useCallback<AuthApi["login"]>(
    async (email, password, remember) => {
      const result = await unwrap(
        api.POST("/api/v1/auth/login", { body: { email, password, remember } }),
      );
      if (result.status === "mfa_required" && result.mfa) {
        return { status: "mfa", challenge: result.mfa };
      }
      if (!result.access_token || !result.expires_in) {
        throw new Error("login: no session in response");
      }
      const me = await signIn({
        access_token: result.access_token,
        expires_in: result.expires_in,
        token_type: result.token_type,
      });
      return { status: "signed-in", me };
    },
    [signIn],
  );

  const verifySecondFactor = useCallback<AuthApi["verifySecondFactor"]>(
    async ({ token, method, code, credential }) => {
      const tokens = await unwrap(
        api.POST("/api/v1/auth/mfa/verify", {
          body: { token, method, code: code ?? null, credential: credential ?? null },
        }),
      );
      return signIn(tokens);
    },
    [signIn],
  );

  const switchCompany = useCallback<AuthApi["switchCompany"]>(
    async (tenantId) => {
      const tokens = await unwrap(
        api.POST("/api/v1/auth/switch-company", { body: { tenant_id: tenantId } }),
      );
      return signIn(tokens);
    },
    [signIn],
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

  const value = useMemo<AuthApi>(
    () => ({
      state,
      login,
      verifySecondFactor,
      signIn,
      switchCompany,
      reloadMe,
      logout,
      changePassword,
    }),
    [state, login, verifySecondFactor, signIn, switchCompany, reloadMe, logout, changePassword],
  );
  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}
