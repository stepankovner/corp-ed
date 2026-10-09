/**
 * Предрендер публичного сайта (ТЗ §1): при сборке (tools/prerender.mjs)
 * страницы сайта рисуются в готовый HTML — их видят поисковики, и они
 * открываются до загрузки скриптов. Рисуются те же маршруты, что в
 * браузере у гостя: главная и «Помощь» — через handle.guest (RequireAuth).
 *
 * В браузере приложение не «оживляет» этот HTML, а рисует заново поверх
 * (main.tsx, createRoot): у вошедшего на тех же адресах другое — его
 * приложение, и готовый HTML у него скрыт до первой отрисовки
 * (index.html, data-session).
 */
/* eslint-disable react-refresh/only-export-components -- точка входа сборки, не модуль страниц */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderToString } from "react-dom/server";
import { createStaticHandler, createStaticRouter, StaticRouterProvider } from "react-router";

import { AuthContext, type AuthApi } from "../auth/context";
import { routes } from "../routes";
import { UiProvider } from "../ui/UiProvider";
import { SITE, SITE_URL, type SiteMeta } from "./meta";

export { SITE_URL };

/** Страницы с готовым HTML: адрес → файл dist/<адрес>/index.html. */
export const PAGES: SiteMeta[] = [
  SITE.home,
  SITE.demo,
  SITE.pricing,
  SITE.security,
  SITE.about,
  SITE.help,
  SITE.privacy,
  SITE.terms,
  SITE.consent,
  SITE.consentCall,
  SITE.offer,
  SITE.refund,
  SITE.cookies,
];

function guestOnly(): never {
  throw new Error("prerender: действие недоступно при сборке");
}

/** Гость: сайт рисуется без входа. */
const GUEST: AuthApi = {
  state: { status: "anonymous" },
  login: guestOnly,
  verifySecondFactor: guestOnly,
  signIn: guestOnly,
  switchCompany: guestOnly,
  reloadMe: guestOnly,
  logout: guestOnly,
  changePassword: guestOnly,
};

export async function render(path: string): Promise<string> {
  const handler = createStaticHandler(routes);
  const context = await handler.query(new Request(new URL(path, SITE_URL)));
  if (context instanceof Response) {
    throw new Error(`prerender ${path}: маршрут ответил ${context.status}`);
  }
  if (context.statusCode !== 200) {
    throw new Error(`prerender ${path}: статус ${context.statusCode}`);
  }
  const router = createStaticRouter(handler.dataRoutes, context);
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return renderToString(
    <QueryClientProvider client={client}>
      <AuthContext.Provider value={GUEST}>
        <UiProvider>
          <StaticRouterProvider router={router} context={context} hydrate={false} />
        </UiProvider>
      </AuthContext.Provider>
    </QueryClientProvider>,
  );
}
