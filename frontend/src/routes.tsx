import { lazy, Suspense, type ReactNode } from "react";
import { Navigate, type RouteObject } from "react-router";

import { PublicOnly, RequireAdmin, RequireAuth } from "./auth/guards";
import { ChatPage } from "./chat/ChatPage";
import { AppShell } from "./layout/AppShell";
import { ChangePasswordPage } from "./pages/ChangePasswordPage";
import { JoinPage } from "./pages/JoinPage";
import { LoginPage } from "./pages/LoginPage";
import { NotFoundPage } from "./pages/NotFoundPage";
import { PageSpinner } from "./ui/Spinner";

// Управление нужно только администраторам — отдельным чанком.
const AdminLayout = lazy(() =>
  import("./layout/AdminLayout").then((m) => ({ default: m.AdminLayout })),
);
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
const UsagePage = lazy(() => import("./admin/UsagePage").then((m) => ({ default: m.UsagePage })));
const AuditPage = lazy(() => import("./admin/AuditPage").then((m) => ({ default: m.AuditPage })));
// Публичные страницы тарифов и записи — отдельным чанком.
const PricingPage = lazy(() =>
  import("./pages/PricingPage").then((m) => ({ default: m.PricingPage })),
);
const CallRequestPage = lazy(() =>
  import("./pages/CallRequestPage").then((m) => ({ default: m.CallRequestPage })),
);
const MySourcesPage = lazy(() =>
  import("./pages/MySourcesPage").then((m) => ({ default: m.MySourcesPage })),
);

function lazyPage(node: ReactNode) {
  return <Suspense fallback={<PageSpinner />}>{node}</Suspense>;
}

export const routes: RouteObject[] = [
  // Ссылка-приглашение открывается и без входа, и под чужой учёткой.
  { path: "/join/:companyCode", element: <JoinPage /> },
  // Тарифы и запись на созвон — для всех, со входом и без.
  { path: "/pricing", element: lazyPage(<PricingPage />) },
  { path: "/pricing/request", element: lazyPage(<CallRequestPage />) },
  {
    element: <PublicOnly />,
    children: [{ path: "/login", element: <LoginPage /> }],
  },
  {
    element: <RequireAuth />,
    children: [
      { path: "/change-password", element: <ChangePasswordPage /> },
      {
        element: <AppShell />,
        children: [
          { path: "/", element: <ChatPage /> },
          { path: "/sources", element: lazyPage(<MySourcesPage />) },
          {
            path: "/admin",
            element: <RequireAdmin />,
            children: [
              {
                element: lazyPage(<AdminLayout />),
                children: [
                  { index: true, element: <Navigate to="documents" replace /> },
                  { path: "documents", element: lazyPage(<DocumentsPage />) },
                  { path: "connectors", element: lazyPage(<ConnectorsPage />) },
                  { path: "connectors/:connectorId", element: lazyPage(<ConnectorPage />) },
                  { path: "users", element: lazyPage(<UsersPage />) },
                  { path: "gaps", element: lazyPage(<GapsPage />) },
                  { path: "glossary", element: lazyPage(<GlossaryPage />) },
                  { path: "usage", element: lazyPage(<UsagePage />) },
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
