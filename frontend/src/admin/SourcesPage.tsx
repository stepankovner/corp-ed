import { Navigate, Outlet, useParams } from "react-router";

import { useDocumentTitle } from "../lib/title";
import { LinkTabs } from "../ui/LinkTabs";
import { Page, PageHeader } from "../ui/Page";

/**
 * «Источники» в одном месте (ТЗ §5): загруженные файлы по папкам и
 * подключённые системы — вкладками, у каждой свой адрес.
 */
export function SourcesPage() {
  useDocumentTitle("Источники");
  return (
    <Page>
      <PageHeader
        label="управление"
        title="Источники"
        description="Где ищет ассистент: загруженные файлы — по папкам с доступом по отделам, и подключённые системы компании."
      />
      <LinkTabs
        label="Источники"
        tabs={[
          { to: "/admin/sources/files", label: "Файлы" },
          { to: "/admin/sources/connections", label: "Подключения" },
        ]}
      />
      <Outlet />
    </Page>
  );
}

/** Старые адреса подключений (ссылки в письмах и закладках) — на новые. */
export function ConnectorRedirect() {
  const { connectorId = "" } = useParams();
  return <Navigate to={`/admin/sources/connections/${connectorId}`} replace />;
}
