import { NavLink, Outlet } from "react-router-dom";

import { useAuth } from "../auth/AuthContext";
import { Button } from "./Button";
import styles from "./Layout.module.css";

const ROLE_LABELS = {
  manager: "Руководитель",
  intern: "Стажёр",
} as const;

export function Layout() {
  const { user, logout } = useAuth();
  const isManager = user?.role === "manager";

  const linkClass = ({ isActive }: { isActive: boolean }) =>
    `${styles.link} ${isActive ? styles.active : ""}`;

  return (
    <div className={styles.shell}>
      <header className={styles.header}>
        <div className={styles.headerInner}>
          <NavLink to="/" className={styles.brand}>
            corp-ed
          </NavLink>

          <nav className={styles.nav}>
            {isManager ? (
              <>
                <NavLink to="/materials" className={linkClass}>
                  Материалы
                </NavLink>
                <NavLink to="/briefs" className={linkClass}>
                  Программы
                </NavLink>
              </>
            ) : null}
            <NavLink to="/chat" className={linkClass}>
              Вопросы
            </NavLink>
          </nav>

          <div className={styles.user}>
            {user ? (
              <>
                <span className={styles.role}>{ROLE_LABELS[user.role]}</span>
                <span>{user.full_name ?? user.email}</span>
              </>
            ) : null}
            <Button variant="quiet" onClick={logout}>
              Выйти
            </Button>
          </div>
        </div>
      </header>

      <main className={styles.main}>
        <Outlet />
      </main>
    </div>
  );
}
