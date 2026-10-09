import { Navigate, Route, Routes } from "react-router";

import { needsStrongFactor, useMe, type Me } from "../auth/context";
import { LinkTabs } from "../ui/LinkTabs";
import { Page, PageHeader } from "../ui/Page";
import { AccountTab } from "./AccountTab";
import { CompaniesTab } from "./CompaniesTab";
import { ConnectionsTab } from "./ConnectionsTab";
import { NotificationsTab } from "./NotificationsTab";
import { ProfileTab } from "./ProfileTab";
import { SecurityTab } from "./SecurityTab";
import { SharedLinksTab } from "./SharedLinksTab";

interface Tab {
  path: string;
  label: string;
}

/**
 * «Мои подключения», «Уведомления» и «Общие ссылки» — про компанию: только
 * когда она выбрана и открыта.
 */
function tabsFor(me: Me): Tab[] {
  const inCompany = me.company !== null && !needsStrongFactor(me);
  return [
    { path: "profile", label: "Профиль" },
    { path: "security", label: "Безопасность" },
    ...(inCompany
      ? [
          { path: "connections", label: "Мои подключения" },
          { path: "notifications", label: "Уведомления" },
          { path: "shared-links", label: "Общие ссылки" },
        ]
      : []),
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
      <LinkTabs
        label="Разделы настроек"
        tabs={tabs.map((tab) => ({ to: `/settings/${tab.path}`, label: tab.label }))}
      />
      <Routes>
        <Route index element={<Navigate to="/settings/profile" replace />} />
        <Route path="profile" element={<ProfileTab />} />
        <Route path="security" element={<SecurityTab />} />
        {connections ? <Route path="connections" element={<ConnectionsTab />} /> : null}
        {connections ? <Route path="notifications" element={<NotificationsTab />} /> : null}
        {connections ? <Route path="shared-links" element={<SharedLinksTab />} /> : null}
        <Route path="companies" element={<CompaniesTab />} />
        <Route path="account" element={<AccountTab />} />
        <Route path="*" element={<Navigate to="/settings/profile" replace />} />
      </Routes>
    </Page>
  );
}
