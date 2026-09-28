import { KeyRound, LogOut } from "lucide-react";
import { useEffect, useId, useRef, useState } from "react";
import { Link } from "react-router";

import { useAuth, useMe } from "../auth/context";
import styles from "./AppShell.module.css";

function initials(name: string | null, email: string): string {
  const source = name?.trim() || email;
  const parts = source.split(/[\s.@_-]+/).filter(Boolean);
  const letters =
    parts.length > 1 ? (parts[0]?.[0] ?? "") + (parts[1]?.[0] ?? "") : source.slice(0, 2);
  return letters.toUpperCase();
}

export function UserMenu() {
  const me = useMe();
  const { logout } = useAuth();
  const [open, setOpen] = useState(false);
  const wrap = useRef<HTMLDivElement>(null);
  const menuId = useId();

  useEffect(() => {
    if (!open) return;
    function onPointer(event: PointerEvent) {
      if (!wrap.current?.contains(event.target as Node)) setOpen(false);
    }
    function onKey(event: KeyboardEvent) {
      if (event.key === "Escape") setOpen(false);
    }
    document.addEventListener("pointerdown", onPointer);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("pointerdown", onPointer);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  return (
    <div className={styles.menuWrap} ref={wrap}>
      <button
        type="button"
        className={styles.avatar}
        aria-haspopup="menu"
        aria-expanded={open}
        aria-controls={menuId}
        aria-label="Профиль"
        onClick={() => setOpen((value) => !value)}
      >
        {initials(me.full_name, me.email)}
      </button>
      {open ? (
        <div className={styles.menu} id={menuId} role="menu">
          <div className={styles.menuHead}>
            <span className={styles.menuName}>{me.full_name || me.email}</span>
            {me.full_name ? <span className="muted">{me.email}</span> : null}
            <span className="mono muted">
              {me.company_name} · {me.role === "admin" ? "администратор" : "сотрудник"}
            </span>
          </div>
          <Link
            className={styles.menuItem}
            to="/change-password"
            role="menuitem"
            onClick={() => setOpen(false)}
          >
            <KeyRound size={16} aria-hidden /> Сменить пароль
          </Link>
          <button
            type="button"
            className={styles.menuItem}
            role="menuitem"
            onClick={() => void logout()}
          >
            <LogOut size={16} aria-hidden /> Выйти
          </button>
        </div>
      ) : null}
    </div>
  );
}
