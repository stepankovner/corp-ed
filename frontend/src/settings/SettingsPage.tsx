import { useEffect, useRef } from "react";
import { Link, Navigate, Route, Routes, useLocation } from "react-router";

import { needsStrongFactor, useMe, type Me } from "../auth/context";
import { Page, PageHeader } from "../ui/Page";
import { AccountTab } from "./AccountTab";
import { CompaniesTab } from "./CompaniesTab";
import { ConnectionsTab } from "./ConnectionsTab";
import { ProfileTab } from "./ProfileTab";
import { SecurityTab } from "./SecurityTab";
import styles from "./Settings.module.css";

// Уведомления (ТЗ §4) — этапом 9.
interface Tab {
  path: string;
  label: string;
}

/** «Мои подключения» — источники компании: только когда она выбрана и открыта. */
function tabsFor(me: Me): Tab[] {
  const inCompany = me.company !== null && !needsStrongFactor(me);
  return [
    { path: "profile", label: "Профиль" },
    { path: "security", label: "Безопасность" },
    ...(inCompany ? [{ path: "connections", label: "Мои подключения" }] : []),
    { path: "companies", label: "Компании" },
    { path: "account", label: "Управление учётной записью" },
  ];
}

/**
 * Настройки учётки (ТЗ §4). Работают и без компании: учётка существует
 * сама по себе. Вкладки — адреса /settings/<вкладка>: на вкладку можно
 * дать ссылку («Настроить защиту» ведёт сразу в «Безопасность»).
 */
export function SettingsPage() {
  const me = useMe();
  const tabs = tabsFor(me);
  const connections = tabs.some((tab) => tab.path === "connections");
  return (
    <Page>
      <PageHeader title="Настройки" />
      <TabBar tabs={tabs} />
      <Routes>
        <Route index element={<Navigate to="/settings/profile" replace />} />
        <Route path="profile" element={<ProfileTab />} />
        <Route path="security" element={<SecurityTab />} />
        {connections ? <Route path="connections" element={<ConnectionsTab />} /> : null}
        <Route path="companies" element={<CompaniesTab />} />
        <Route path="account" element={<AccountTab />} />
        <Route path="*" element={<Navigate to="/settings/profile" replace />} />
      </Routes>
    </Page>
  );
}

function TabBar({ tabs }: { tabs: Tab[] }) {
  const { pathname } = useLocation();
  const active = useRef<HTMLAnchorElement>(null);

  // На телефоне лента вкладок шире экрана: открытая вкладка — в поле зрения.
  useEffect(() => {
    active.current?.scrollIntoView({ block: "nearest", inline: "nearest" });
  }, [pathname]);

  return (
    <nav className={styles.tabs} aria-label="Разделы настроек">
      {tabs.map((tab) => {
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
