import { useEffect } from "react";
import {
  isRouteErrorResponse,
  Link,
  Links,
  Meta,
  Outlet,
  redirect,
  Scripts,
  ScrollRestoration,
  useLocation,
  useMatches,
  useRouteLoaderData,
} from "react-router";

import type { Route } from "./+types/root";
import "./app.css";
import { SiteFooter, SiteHeader } from "./components/site-chrome";
import { internal } from "./lib/api.server";
import { HTML_LANG, langOf, PREFIX, TRANSLATORS } from "./lib/i18n";
import { useNonce } from "./lib/nonce";
import { PREPAINT_SCRIPT } from "./lib/theme";
import type { Chrome, Seo } from "./lib/types";

export const links: Route.LinksFunction = () => [{ rel: "icon", href: "/favicon.svg", type: "image/svg+xml" }];

/** 缓存头按路径与状态码统一设（第 15.5 节），文档与单页导航的 .data 请求一样：列表 300 秒，详情、商家、关于 600 秒，
 *  404 为 60 秒；状态页每次现算，不缓存。 */
function cachePolicy(pathname: string, status: number): string {
  if (status >= 500) return "no-store";
  if (status === 404) return "public, max-age=60";
  const path = pathname.replace(/^\/en(?=\/|$)/, "") || "/";
  if (path === "/status") return "no-store";
  return /^\/(deal|vendor)\//.test(path) || path === "/about" ? "public, max-age=600" : "public, max-age=300";
}

export const middleware: Route.MiddlewareFunction[] = [
  async ({ request, url }, next) => {
    // 只对文档请求整理地址：/en 跳到 /en/（D58），其余结尾的斜杠去掉，免得同一页有两个地址
    if (!new URL(request.url).pathname.endsWith(".data")) {
      const { pathname, search } = url;
      const target =
        pathname === "/en" ? "/en/" : pathname.length > 1 && pathname.endsWith("/") && pathname !== "/en/" ? pathname.slice(0, -1) : null;
      if (target) throw redirect(target + search, { status: 307, headers: { "Cache-Control": "public, max-age=300" } });
    }
    const response = await next();
    response.headers.set("Cache-Control", cachePolicy(url.pathname, response.status));
    return response;
  },
];

export async function loader({ request, url }: Route.LoaderArgs) {
  const lang = langOf(url.pathname);
  return { chrome: await internal<Chrome>(`/${lang}/chrome`, request.signal) };
}

export function Layout({ children }: { children: React.ReactNode }) {
  const { pathname } = useLocation();
  const lang = langOf(pathname);
  const t = TRANSLATORS[lang];
  const nonce = useNonce();
  const root = useRouteLoaderData<typeof loader>("root");
  const leaf = useMatches().at(-1)?.loaderData as { seo?: Seo } | undefined;
  useEffect(() => {
    // 水合完成的标记：之后主题按钮才可点（在此之前占位不可见）
    document.documentElement.setAttribute("data-hydrated", "");
  }, []);
  return (
    <html lang={HTML_LANG[lang]} suppressHydrationWarning>
      <head>
        <meta charSet="utf-8" />
        <meta name="viewport" content="width=device-width, initial-scale=1" />
        <meta name="color-scheme" content="light dark" />
        <script nonce={nonce} suppressHydrationWarning dangerouslySetInnerHTML={{ __html: PREPAINT_SCRIPT }} />
        <Meta />
        <Links />
      </head>
      <body>
        <SiteHeader lang={lang} t={t} pathname={pathname} chrome={root?.chrome} alternates={leaf?.seo?.alternates} />
        <main className="page">{children}</main>
        <SiteFooter lang={lang} t={t} chrome={root?.chrome} />
        <ScrollRestoration />
        <Scripts />
      </body>
    </html>
  );
}

export default function App() {
  return <Outlet />;
}

export function meta({ error, location }: Route.MetaArgs) {
  if (!error) return [];
  const t = TRANSLATORS[langOf(location.pathname)];
  return [{ title: t(isRouteErrorResponse(error) && error.status === 404 ? "notfound.title" : "error.title") }];
}

/** 发布新版本后，旧页面在客户端导航时可能取不到旧分块或 .data（网络错误）：整页刷新一次换成新版本。同一地址在
 *  本次会话里只刷新一次，免得真断网时反复刷新。路由模块加载失败时 React Router 自己也会刷新。 */
const STALE = /dynamically imported module|Importing a module script failed|Failed to fetch|NetworkError|Load failed/;

function useReloadOnce(error: unknown) {
  useEffect(() => {
    if (!(error instanceof Error) || !STALE.test(error.message)) return;
    const key = `reloaded:${window.location.pathname}${window.location.search}`;
    try {
      if (sessionStorage.getItem(key)) return;
      sessionStorage.setItem(key, "1");
    } catch {
      return;
    }
    window.location.reload();
  }, [error]);
}

export function ErrorBoundary({ error }: Route.ErrorBoundaryProps) {
  const lang = langOf(useLocation().pathname);
  const t = TRANSLATORS[lang];
  const notFound = isRouteErrorResponse(error) && error.status === 404;
  if (!notFound && !isRouteErrorResponse(error)) console.error(error);
  useReloadOnce(error);
  return (
    <div className="notice">
      <h1>{t(notFound ? "notfound.text" : "error.text")}</h1>
      <p>
        <Link to={`${PREFIX[lang]}/`}>{t("notfound.back")}</Link>
      </p>
    </div>
  );
}
