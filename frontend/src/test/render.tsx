import { QueryClientProvider } from "@tanstack/react-query";
import { render } from "@testing-library/react";
import { createMemoryRouter, RouterProvider } from "react-router";

import { setSession } from "../api/session";
import { AuthProvider } from "../auth/AuthProvider";
import { makeQueryClient } from "../queryClient";
import { routes } from "../routes";
import { UiProvider } from "../ui/UiProvider";

/** Всё приложение на заданном адресе; signedIn — с access-токеном в памяти вкладки. */
export function renderApp(path: string, { signedIn = true } = {}) {
  if (signedIn) {
    setSession({ accessToken: "access-1", expiresAt: Date.now() + 600_000 });
  }
  const router = createMemoryRouter(routes, { initialEntries: [path] });
  const client = makeQueryClient();
  client.setDefaultOptions({ queries: { ...client.getDefaultOptions().queries, retry: false } });
  const utils = render(
    <QueryClientProvider client={client}>
      <AuthProvider>
        <UiProvider>
          <RouterProvider router={router} />
        </UiProvider>
      </AuthProvider>
    </QueryClientProvider>,
  );
  return { ...utils, router };
}
