import { useEffect, useRef } from "react";
import { Link, Navigate, Route, Routes, useLocation } from "react-router";

import { Page, PageHeader } from "../ui/Page";
import { AccountTab } from "./AccountTab";
import { CompaniesTab } from "./CompaniesTab";
import { ProfileTab } from "./ProfileTab";
import { SecurityTab } from "./SecurityTab";
import styles from "./Settings.module.css";

// Уведомления и «Мои подключения» (ТЗ §4) — следующими этапами.
const TABS = [
  { path: "profile", label: "Профиль" },
  { path: "security", label: "Безопасность" },
  { path: "companies", label: "Компании" },
  { path: "account", label: "Управление учётной записью" },
] as const;

/**
 * Настройки учётки (ТЗ §4). Работают и без компании: учётка существует
 * сама по себе. Вкладки — адреса /settings/<вкладка>: на вкладку можно
 * дать ссылку («Настроить защиту» ведёт сразу в «Безопасность»).
 */
export function SettingsPage() {
  return (
    <Page>
      <PageHeader title="Настройки" />
      <TabBar />
      <Routes>
        <Route index element={<Navigate to="/settings/profile" replace />} />
        <Route path="profile" element={<ProfileTab />} />
        <Route path="security" element={<SecurityTab />} />
        <Route path="companies" element={<CompaniesTab />} />
        <Route path="account" element={<AccountTab />} />
        <Route path="*" element={<Navigate to="/settings/profile" replace />} />
      </Routes>
    </Page>
  );
}

function TabBar() {
  const { pathname } = useLocation();
  const active = useRef<HTMLAnchorElement>(null);

  // На телефоне лента вкладок шире экрана: открытая вкладка — в поле зрения.
  useEffect(() => {
    active.current?.scrollIntoView({ block: "nearest", inline: "nearest" });
  }, [pathname]);

  return (
    <nav className={styles.tabs} aria-label="Разделы настроек">
      {TABS.map((tab) => {
        const to = `/settings/${tab.path}`;
        const current = pathname === to;
        return (
          <Link
            key={tab.path}
            ref={current ? active : undefined}
            to={to}
            className={styles.tab}
            aria-current={current ? "page" : undefined}
          >
            {tab.label}
          </Link>
        );
      })}
    </nav>
  );
}
