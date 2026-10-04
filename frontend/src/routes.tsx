import { lazy, Suspense, type ReactNode } from "react";
import { Navigate, useLocation, type RouteObject } from "react-router";

import { PublicOnly, RequireAdmin, RequireAuth, RequireCompany, RequireStaff } from "./auth/guards";
import { ChatPage } from "./chat/ChatPage";
import { AppShell } from "./layout/AppShell";
import { ChangePasswordPage } from "./pages/ChangePasswordPage";
import { ConfirmEmailPage, RevertEmailPage } from "./pages/EmailChangePages";
import { ForgotPasswordPage } from "./pages/ForgotPasswordPage";
import { HomePage } from "./pages/HomePage";
import { JoinPage } from "./pages/JoinPage";
import { LoginPage } from "./pages/LoginPage";
import { NotFoundPage } from "./pages/NotFoundPage";
import { PrivacyPage } from "./pages/PrivacyPage";
import { RegisterPage } from "./pages/RegisterPage";
import { ResetPasswordPage } from "./pages/ResetPasswordPage";
import { VerifyEmailPage } from "./pages/VerifyEmailPage";
import { PageSpinner } from "./ui/Spinner";

// Управление нужно только администраторам — отдельными чанками; его
// разделы — в боковой панели оболочки (layout/Sidebar.tsx).
const DocumentsPage = lazy(() =>
  import("./admin/DocumentsPage").then((m) => ({ default: m.DocumentsPage })),
);
const ConnectorsPage = lazy(() =>
  import("./admin/ConnectorsPage").then((m) => ({ default: m.ConnectorsPage })),
);
const ConnectorPage = lazy(() =>
  import("./admin/ConnectorPage").then((m) => ({ default: m.ConnectorPage })),
);
const UsersPage = lazy(() => import("./admin/UsersPage").then((m) => ({ default: m.UsersPage })));
const GapsPage = lazy(() => import("./admin/GapsPage").then((m) => ({ default: m.GapsPage })));
const GlossaryPage = lazy(() =>
  import("./admin/GlossaryPage").then((m) => ({ default: m.GlossaryPage })),
);
const OverviewPage = lazy(() =>
  import("./admin/OverviewPage").then((m) => ({ default: m.OverviewPage })),
);
const SourcesPage = lazy(() =>
  import("./admin/SourcesPage").then((m) => ({ default: m.SourcesPage })),
);
const ConnectorRedirect = lazy(() =>
  import("./admin/SourcesPage").then((m) => ({ default: m.ConnectorRedirect })),
);
const TariffPage = lazy(() =>
  import("./admin/TariffPage").then((m) => ({ default: m.TariffPage })),
);
const CompanySettingsPage = lazy(() =>
  import("./admin/CompanySettingsPage").then((m) => ({ default: m.CompanySettingsPage })),
);
const AuditPage = lazy(() => import("./admin/AuditPage").then((m) => ({ default: m.AuditPage })));
// Публичные страницы тарифов и записи — отдельным чанком.
const PricingPage = lazy(() =>
  import("./pages/PricingPage").then((m) => ({ default: m.PricingPage })),
);
const CallRequestPage = lazy(() =>
  import("./pages/CallRequestPage").then((m) => ({ default: m.CallRequestPage })),
);
const PeoplePage = lazy(() =>
  import("./people/PeoplePage").then((m) => ({ default: m.PeoplePage })),
);
const DepartmentsPage = lazy(() =>
  import("./admin/DepartmentsPage").then((m) => ({ default: m.DepartmentsPage })),
);
const SuggestionsPage = lazy(() =>
  import("./admin/SuggestionsPage").then((m) => ({ default: m.SuggestionsPage })),
);
const SharedPage = lazy(() => import("./chat/SharedPage").then((m) => ({ default: m.SharedPage })));
// Наша панель (ТЗ §9): вкладки — вложенные маршруты в staff/.
const StaffPage = lazy(() => import("./staff/StaffPage").then((m) => ({ default: m.StaffPage })));
const RequestsTab = lazy(() =>
  import("./staff/RequestsTab").then((m) => ({ default: m.RequestsTab })),
);
const CompaniesTab = lazy(() =>
  import("./staff/CompaniesTab").then((m) => ({ default: m.CompaniesTab })),
);
const SpendTab = lazy(() => import("./staff/SpendTab").then((m) => ({ default: m.SpendTab })));
const PeopleTab = lazy(() => import("./staff/PeopleTab").then((m) => ({ default: m.PeopleTab })));
const LeadsTab = lazy(() => import("./staff/LeadsTab").then((m) => ({ default: m.LeadsTab })));
const SupportTab = lazy(() =>
  import("./staff/SupportTab").then((m) => ({ default: m.SupportTab })),
);
const HelpPage = lazy(() => import("./help/HelpPage").then((m) => ({ default: m.HelpPage })));
// Настройки учётки (ТЗ §4): вкладки — вложенные маршруты в settings/.
const SettingsPage = lazy(() =>
  import("./settings/SettingsPage").then((m) => ({ default: m.SettingsPage })),
);

function SourcesRedirect() {
  const { search } = useLocation();
  return <Navigate to={`/settings/connections${search}`} replace />;
}

function lazyPage(node: ReactNode) {
  return <Suspense fallback={<PageSpinner />}>{node}</Suspense>;
}

export const routes: RouteObject[] = [
  // Приглашение открывается и без входа (сохраним и вернёмся после него),
  // и под учёткой — тогда вступление одной кнопкой.
  { path: "/join", element: <JoinPage /> },
  // Ссылки из писем: работают со входом и без — письмо могли открыть на
  // другом устройстве.
  { path: "/verify-email", element: <VerifyEmailPage /> },
  { path: "/reset-password", element: <ResetPasswordPage /> },
  { path: "/confirm-email", element: <ConfirmEmailPage /> },
  { path: "/revert-email", element: <RevertEmailPage /> },
  { path: "/privacy", element: <PrivacyPage /> },
  // Тарифы и запись на созвон — для всех, со входом и без.
  { path: "/pricing", element: lazyPage(<PricingPage />) },
  { path: "/pricing/request", element: lazyPage(<CallRequestPage />) },
  {
    element: <PublicOnly />,
    children: [
      { path: "/login", element: <LoginPage /> },
      { path: "/register", element: <RegisterPage /> },
      { path: "/forgot-password", element: <ForgotPasswordPage /> },
    ],
  },
  {
    element: <RequireAuth />,
    children: [
      { path: "/change-password", element: <ChangePasswordPage /> },
      {
        element: <AppShell />,
        children: [
          { path: "/", element: <HomePage /> },
          { path: "/settings/*", element: lazyPage(<SettingsPage />) },
          // Помощь и «Написать в поддержку» (ТЗ §8) — и без компании.
          { path: "/help", element: lazyPage(<HelpPage />) },
          // Команда kronto — и без своей компании.
          {
            path: "/staff",
            element: <RequireStaff />,
            children: [
              {
                element: lazyPage(<StaffPage />),
                children: [
                  { index: true, element: <Navigate to="/staff/requests" replace /> },
                  { path: "requests", element: lazyPage(<RequestsTab />) },
                  { path: "companies", element: lazyPage(<CompaniesTab />) },
                  { path: "spend", element: lazyPage(<SpendTab />) },
                  { path: "people", element: lazyPage(<PeopleTab />) },
                  { path: "leads", element: lazyPage(<LeadsTab />) },
                  { path: "support", element: lazyPage(<SupportTab />) },
                ],
              },
            ],
          },
          {
            element: <RequireCompany />,
            children: [
              // Возврат с портала OAuth (CONNECTOR_OAUTH_RETURN_URL) — сюда;
              // «Мои подключения» теперь в настройках (ТЗ §4).
              { path: "/sources", element: <SourcesRedirect /> },
              // Диалоги на сервере (ТЗ §6): свой — по id, коллеги — по ссылке.
              { path: "/c/:conversationId", element: <ChatPage /> },
              { path: "/shared/:token", element: lazyPage(<SharedPage />) },
              { path: "/people", element: lazyPage(<PeoplePage />) },
              {
                path: "/admin",
                element: <RequireAdmin />,
                children: [
                  { index: true, element: <Navigate to="overview" replace /> },
                  { path: "overview", element: lazyPage(<OverviewPage />) },
                  // «Источники» в одном месте (ТЗ §5): файлы и подключения — вкладками.
                  {
                    path: "sources",
                    element: lazyPage(<SourcesPage />),
                    children: [
                      { index: true, element: <Navigate to="files" replace /> },
                      { path: "files", element: lazyPage(<DocumentsPage />) },
                      { path: "connections", element: lazyPage(<ConnectorsPage />) },
                    ],
                  },
                  {
                    path: "sources/connections/:connectorId",
                    element: lazyPage(<ConnectorPage />),
                  },
                  // Прежние адреса — из закладок и писем.
                  { path: "documents", element: <Navigate to="/admin/sources/files" replace /> },
                  {
                    path: "connectors",
                    element: <Navigate to="/admin/sources/connections" replace />,
                  },
                  { path: "connectors/:connectorId", element: lazyPage(<ConnectorRedirect />) },
                  { path: "usage", element: <Navigate to="/admin/tariff" replace /> },
                  { path: "users", element: lazyPage(<UsersPage />) },
                  { path: "departments", element: lazyPage(<DepartmentsPage />) },
                  { path: "suggestions", element: lazyPage(<SuggestionsPage />) },
                  { path: "gaps", element: lazyPage(<GapsPage />) },
                  { path: "glossary", element: lazyPage(<GlossaryPage />) },
                  { path: "tariff", element: lazyPage(<TariffPage />) },
                  { path: "settings", element: lazyPage(<CompanySettingsPage />) },
                  { path: "audit", element: lazyPage(<AuditPage />) },
                ],
              },
            ],
          },
          { path: "*", element: <NotFoundPage /> },
        ],
      },
    ],
  },
];
