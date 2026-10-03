import { useEffect, useState } from "react";
import { useLocation, useNavigate } from "react-router";

/**
 * Секрет из ссылки письма или приглашения — после «#»: фрагмент не уходит
 * на сервер и в журналы (ТЗ §3). Забираем его в состояние страницы и
 * убираем из адресной строки, чтобы не остался в истории и закладках.
 *
 * key — имя параметра (#token=…); null — весь фрагмент (/join#<токен>).
 */
export function useHashSecret(key: string | null): string | null {
  const location = useLocation();
  const navigate = useNavigate();
  const [secret] = useState(() => readHash(location.hash, key));

  useEffect(() => {
    if (!location.hash) return;
    void navigate(
      { pathname: location.pathname, search: location.search },
      { replace: true, state: location.state as unknown },
    );
  }, [location.hash, location.pathname, location.search, location.state, navigate]);

  return secret;
}

export function readHash(hash: string, key: string | null): string | null {
  const raw = hash.replace(/^#/, "");
  if (!raw) return null;
  if (key === null) return decodeURIComponent(raw).trim() || null;
  const value = new URLSearchParams(raw).get(key);
  return value?.trim() || null;
}
