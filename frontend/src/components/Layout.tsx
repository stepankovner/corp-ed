import { NavLink, Outlet, useLocation } from "react-router-dom";

import { useAuth } from "../auth/AuthContext";
import styles from "./Layout.module.css";

const ROLE_LABELS = {
  manager: "Руководитель",
  intern: "Стажёр",
} as const;

export function Layout() {
  const { user, logout } = useAuth();
  const location = useLocation();
  const isManager = user?.role === "manager";

  const linkClass = ({ isActive }: { isActive: boolean }) =>
    `${styles.link} ${isActive ? styles.active : ""}`;

  // Бриф и готовая программа — один раздел «Программы»: подсветка в шапке
  // не должна гаснуть при переходе от анкеты к результату.
  const inPrograms = location.pathname.startsWith("/programs");

  return (
    <div className={styles.shell}>
      <header className={styles.bar}>
        <div className={styles.barInner}>
          <NavLink to="/" className={styles.brand}>
            <span className={styles.wordmark}>kronto</span>
            {user?.company_name ? (
              <span className={styles.company}>{user.company_name}</span>
            ) : null}
          </NavLink>

          <nav className={styles.nav}>
            {isManager ? (
              <>
                <NavLink to="/materials" className={linkClass}>
                  Материалы
                </NavLink>
                <NavLink
                  to="/programs"
                  className={`${styles.link} ${
                    inPrograms ? styles.active : ""
                  }`}
                >
                  Программы
                </NavLink>
                <NavLink to="/chat" className={linkClass}>
                  Чат
                </NavLink>
              </>
            ) : (
              <NavLink to="/my" className={linkClass}>
                Моя стажировка
              </NavLink>
            )}
          </nav>

          <div className={styles.user}>
            {user ? (
              <div className={styles.person}>
                <span className={styles.name}>
                  {user.full_name ?? user.email}
                </span>
                <span className={styles.role}>{ROLE_LABELS[user.role]}</span>
              </div>
            ) : null}
            <button type="button" className={styles.logout} onClick={logout}>
              Выйти
            </button>
          </div>
        </div>
      </header>

      <main className={styles.main}>
        <Outlet />
      </main>
    </div>
  );
}
