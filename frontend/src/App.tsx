import { QueryClientProvider } from "@tanstack/react-query";
import { useState } from "react";
import { createBrowserRouter, RouterProvider } from "react-router";

import { AuthProvider } from "./auth/AuthProvider";
import { makeQueryClient } from "./queryClient";
import { routes } from "./routes";
import { UiProvider } from "./ui/UiProvider";

export function App() {
  const [queryClient] = useState(makeQueryClient);
  const [router] = useState(() => createBrowserRouter(routes));
  return (
    <QueryClientProvider client={queryClient}>
      <AuthProvider>
        <UiProvider>
          <RouterProvider router={router} />
        </UiProvider>
      </AuthProvider>
    </QueryClientProvider>
  );
}
