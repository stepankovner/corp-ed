import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

// В разработке API — на uvicorn рядом; в продакшене nginx отдаёт сборку
// и проксирует /api на тот же адрес (один origin — без CORS).
const backend = process.env.KRONTO_BACKEND ?? "http://127.0.0.1:8000";

export default defineConfig({
  plugins: [react()],
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
    sourcemap: true,
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
    // Стили в тестах не нужны; ?raw — текст файла для проверок токенов.
    css: { include: [/\.css\?raw$/], modules: { classNameStrategy: "non-scoped" } },
    include: ["src/**/*.test.{ts,tsx}"],
  },
});
