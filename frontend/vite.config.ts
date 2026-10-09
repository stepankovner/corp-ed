import { existsSync } from "node:fs";
import { join } from "node:path";

import react from "@vitejs/plugin-react";
import type { Plugin } from "vite";
import { defineConfig } from "vitest/config";

/**
 * `vite preview` (e2e) отдаёт сборку как nginx.conf: страницы сайта —
 * готовый HTML dist/<адрес>/index.html, остальные адреса — оболочка
 * приложения app.html. Без этого preview отдал бы на всё index.html.
 */
function siteRouting(): Plugin {
  return {
    name: "kronto-site-routing",
    configurePreviewServer(server) {
      const dist = join(server.config.root, server.config.build.outDir);
      server.middlewares.use((req, _res, next) => {
        const [path = "/", query] = (req.url ?? "/").split("?");
        const accept = req.headers.accept ?? "";
        if ((req.method === "GET" || req.method === "HEAD") && accept.includes("text/html")) {
          const page = join(dist, path, "index.html");
          const file = join(dist, path);
          if (!existsSync(page) && !(path !== "/" && existsSync(file) && path.includes("."))) {
            req.url = `/app.html${query ? `?${query}` : ""}`;
          } else if (existsSync(page) && path !== "/") {
            req.url = `${path.replace(/\/$/, "")}/index.html${query ? `?${query}` : ""}`;
          }
        }
        next();
      });
    },
  };
}

// В разработке API — на uvicorn рядом; в продакшене nginx отдаёт сборку
// и проксирует /api на тот же адрес (один origin — без CORS).
const backend = process.env.KRONTO_BACKEND ?? "http://127.0.0.1:8000";

export default defineConfig({
  plugins: [react(), siteRouting()],
  server: {
    port: 5173,
    proxy: {
      "/api": { target: backend, changeOrigin: false },
      "/health": { target: backend },
    },
  },
  preview: {
    port: 4173,
    proxy: {
      "/api": { target: backend, changeOrigin: false },
    },
  },
  build: {
    target: "es2022",
    // Карты собираются (разбор ошибок по стеку), но ссылки на них в
    // скриптах нет, а nginx отдаёт на *.map 404.
    sourcemap: "hidden",
    rolldownOptions: {
      output: {
        // Библиотеки меняются реже кода приложения — отдельные чанки
        // переживают выкладки в кеше браузера.
        codeSplitting: {
          groups: [
            {
              name: "react",
              test: /node_modules[\\/](react|react-dom|scheduler|react-router|@tanstack)[\\/]/,
            },
            {
              name: "markdown",
              test: /node_modules[\\/](react-markdown|remark-.*|micromark.*|mdast-.*|hast-.*|unist-.*|unified|vfile.*|property-information|decode-named-character-reference|character-entities.*|space-separated-tokens|comma-separated-tokens|html-url-attributes|estree-util-.*|devlop|bail|trough|trim-lines|zwitch|longest-streak|markdown-table|ccount|escape-string-regexp|is-plain-obj|style-to-js|style-to-object|inline-style-parser)[\\/]/,
            },
          ],
        },
      },
    },
  },
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./src/test/setup.ts"],
    // Под нагрузкой CI (файлы параллельно) длинный сценарий страницы не
    // укладывается в 5 с по умолчанию; ожидания findBy — 5 с (setup.ts).
    testTimeout: 20_000,
    // Стили в тестах не нужны; ?raw — текст файла для проверок токенов.
    css: { include: [/\.css\?raw$/], modules: { classNameStrategy: "non-scoped" } },
    include: ["src/**/*.test.{ts,tsx}"],
  },
});
