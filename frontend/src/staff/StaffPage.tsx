import { useQuery } from "@tanstack/react-query";
import { Outlet } from "react-router";

import { api, unwrap } from "../api/client";
import { formatNumber, plural } from "../lib/format";
import { LinkTabs } from "../ui/LinkTabs";
import { Page, PageHeader } from "../ui/Page";
import { STAFF_KEY } from "./keys";

/**
 * Наша панель (ТЗ §9): команда kronto вместо команд на сервере — заявки
 * на компании, компании (тариф, места, пилот, пауза), расход на модели,
 * помощь со входом, заявки на созвон. Заголовок вкладки браузера ставят
 * сами вкладки.
 */
export function StaffPage() {
  const overview = useQuery({
    queryKey: [...STAFF_KEY, "overview"],
    queryFn: () => unwrap(api.GET("/api/v1/staff/overview")),
  });
  const data = overview.data;
  return (
    <Page wide>
      <PageHeader
        label="команда kronto"
        title="Панель kronto"
        description={
          data
            ? [
                `${formatNumber(data.companies)} ${plural(data.companies, "компания", "компании", "компаний")}, работают ${formatNumber(data.active_companies)}`,
                data.requests_new
                  ? `новых заявок: ${formatNumber(data.requests_new)}`
                  : "новых заявок нет",
                data.pilots_ending
                  ? `пилот заканчивается: ${formatNumber(data.pilots_ending)}`
                  : null,
                `учёток: ${formatNumber(data.accounts)}`,
              ]
                .filter(Boolean)
                .join(" · ")
            : "Заявки, компании, расход на модели и помощь со входом."
        }
      />
      <LinkTabs
        label="Разделы панели"
        tabs={[
          { to: "/staff/requests", label: "Заявки" },
          { to: "/staff/companies", label: "Компании" },
          { to: "/staff/spend", label: "Расход" },
          { to: "/staff/people", label: "Люди" },
          { to: "/staff/leads", label: "Созвоны" },
        ]}
      />
      <Outlet />
    </Page>
  );
}
