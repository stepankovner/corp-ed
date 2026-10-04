// Готовый HTML публичного сайта (ТЗ §1) — после `vite build` и SSR-сборки
// src/site/prerender.tsx (npm run build). Из dist/index.html получаются:
//   app.html             — пустая оболочка приложения (nginx отдаёт её на
//                          адресах приложения), noindex;
//   <адрес>/index.html   — страницы сайта с готовой разметкой, заголовком,
//                          описанием и каноническим адресом;
//   sitemap.xml, robots.txt, robots-closed.txt — для поисковиков.
// Индексировать или нет, решает nginx по имени хоста (nginx.conf): образ
// один для стенда и боя, стенд закрыт от поисковиков.
import { mkdir, readFile, rm, writeFile } from "node:fs/promises";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");
const dist = join(root, "dist");
const ssr = join(root, "dist-ssr");

const { PAGES, SITE_URL, render } = await import(join(ssr, "prerender.js"));

const ROBOTS_META = '<meta name="robots" content="noindex" />';
const TITLE = /<title>[^<]*<\/title>/;
const ROOT = '<div id="root"></div>';

const template = await readFile(join(dist, "index.html"), "utf8");
for (const marker of [ROBOTS_META, ROOT]) {
  if (!template.includes(marker)) throw new Error(`index.html: нет ${marker}`);
}
if (!TITLE.test(template)) throw new Error("index.html: нет <title>");

function escape(text) {
  return text
    .replaceAll("&", "&amp;")
    .replaceAll('"', "&quot;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;");
}

// Оболочка приложения — как собрал Vite: страницы приложения не индексируются.
await writeFile(join(dist, "app.html"), template);

for (const page of PAGES) {
  const url = new URL(page.path, SITE_URL).href;
  const head = [
    `<title>${escape(page.title)}</title>`,
    `<meta name="description" content="${escape(page.description)}" />`,
    `<link rel="canonical" href="${url}" />`,
    `<meta property="og:type" content="website" />`,
    `<meta property="og:site_name" content="kronto" />`,
    `<meta property="og:locale" content="ru_RU" />`,
    `<meta property="og:title" content="${escape(page.title)}" />`,
    `<meta property="og:description" content="${escape(page.description)}" />`,
    `<meta property="og:url" content="${url}" />`,
    `<meta property="og:image" content="${new URL("/og-image.png", SITE_URL).href}" />`,
  ].join("\n    ");
  const body = await render(page.path);
  const html = template
    .replace(ROBOTS_META, "")
    .replace(TITLE, head)
    .replace(ROOT, `<div id="root"><div data-prerender>${body}</div></div>`);
  const file =
    page.path === "/" ? join(dist, "index.html") : join(dist, page.path.slice(1), "index.html");
  await mkdir(dirname(file), { recursive: true });
  await writeFile(file, html);
  console.log(`prerender ${page.path} → ${file.slice(root.length + 1)} (${html.length} байт)`);
}

const urls = PAGES.map(
  (page) => `  <url><loc>${new URL(page.path, SITE_URL).href}</loc></url>`,
).join("\n");
await writeFile(
  join(dist, "sitemap.xml"),
  `<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n${urls}\n</urlset>\n`,
);
await writeFile(
  join(dist, "robots.txt"),
  `User-agent: *\nDisallow: /api/\nAllow: /\n\nSitemap: ${new URL("/sitemap.xml", SITE_URL).href}\n`,
);
await writeFile(join(dist, "robots-closed.txt"), "User-agent: *\nDisallow: /\n");

await rm(ssr, { recursive: true, force: true });
// BroadcastChannel сессии (api/session.ts) держит процесс — выходим явно.
process.exit(0);
