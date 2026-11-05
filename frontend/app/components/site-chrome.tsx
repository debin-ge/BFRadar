import { Link } from "react-router";

import { HTML_LANG, type Lang, PREFIX, type T } from "~/lib/i18n";
import { cycleTheme } from "~/lib/theme";
import type { Chrome, Seo } from "~/lib/types";

/** 当前选择由 CSS 按 html[data-theme] 显示，服务端与浏览器渲染同一份标记；没有脚本时整个按钮隐藏（跟随系统）。 */
function ThemeToggle({ t }: { t: T }) {
  return (
    <button type="button" className="theme-toggle" onClick={cycleTheme} title={t("theme.switch")}>
      <svg data-theme-icon="system" viewBox="0 0 24 24" aria-hidden="true">
        <rect x="3" y="4" width="18" height="12" rx="2" />
        <path d="M8 20h8M12 16v4" />
      </svg>
      <svg data-theme-icon="light" viewBox="0 0 24 24" aria-hidden="true">
        <circle cx="12" cy="12" r="4" />
        <path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4" />
      </svg>
      <svg data-theme-icon="dark" viewBox="0 0 24 24" aria-hidden="true">
        <path d="M20 14.5A8 8 0 0 1 9.5 4a8 8 0 1 0 10.5 10.5z" />
      </svg>
      <span data-theme-label="system">{t("theme.system")}</span>
      <span data-theme-label="light">{t("theme.light")}</span>
      <span data-theme-label="dark">{t("theme.dark")}</span>
    </button>
  );
}

/** 品牌标：线框雷达，扫描线慢慢转（减少动态时静止）。颜色跟随 currentColor。 */
function BrandMark() {
  return (
    <svg className="brand-mark" viewBox="0 0 32 32" aria-hidden="true">
      <circle className="ring" cx="16" cy="16" r="14" />
      <circle className="ring" cx="16" cy="16" r="8" />
      <path className="axis" d="M16 2v28M2 16h28" />
      <g className="sweep">
        <path className="beam" d="M16 16V2" />
      </g>
      <circle className="blip" cx="21.5" cy="10" r="2" />
    </svg>
  );
}

type Props = { lang: Lang; t: T; pathname: string; chrome?: Chrome; alternates?: Seo["alternates"] };

export function SiteHeader({ lang, t, pathname, chrome, alternates }: Props) {
  const prefix = PREFIX[lang];
  const path = pathname.slice(prefix.length) || "/";
  const section = path === "/" ? "deals" : path.startsWith("/plans") ? "plans" : path.startsWith("/vendor") ? "vendors" : path.startsWith("/codes") ? "codes" : "";
  return (
    <header className="masthead">
      <div className="masthead-inner">
        <Link className="brand" to={`${prefix}/`}>
          <BrandMark />
          {t("site.name")}
        </Link>
        <nav className="sections">
          <Link to={`${prefix}/`} aria-current={section === "deals" ? "page" : undefined}>
            {t("nav.deals")}
          </Link>
          <Link to={`${prefix}/plans`} aria-current={section === "plans" ? "page" : undefined}>
            {t("nav.plans")}
          </Link>
          <Link to={`${prefix}/vendors`} aria-current={section === "vendors" ? "page" : undefined}>
            {t("nav.vendors")}
          </Link>
          <Link to={`${prefix}/codes`} aria-current={section === "codes" ? "page" : undefined}>
            {t("nav.codes")}
          </Link>
        </nav>
        {chrome && <p className="freshness">{chrome.freshness}</p>}
        <div className="masthead-tools">
          {alternates && (
            <nav className="langs" aria-label={t("lang.label")}>
              {(["zh", "en"] as const).map((code) => (
                <Link
                  key={code}
                  to={alternates[code]}
                  hrefLang={HTML_LANG[code]}
                  lang={HTML_LANG[code]}
                  aria-current={code === lang ? "true" : undefined}
                >
                  {t(`lang.${code}`)}
                </Link>
              ))}
            </nav>
          )}
          <ThemeToggle t={t} />
        </div>
      </div>
    </header>
  );
}

export function SiteFooter({ lang, t, chrome }: Omit<Props, "pathname" | "alternates">) {
  return (
    <footer className="colophon">
      <div className="colophon-inner">
        {chrome && <p>{chrome.footer}</p>}
        <p>{t("footer.names")}</p>
        <nav>
          <Link to={`${PREFIX[lang]}/vendors`}>{t("footer.vendors")}</Link>
          <Link to={`${PREFIX[lang]}/codes`}>{t("footer.codes")}</Link>
          <Link to={`${PREFIX[lang]}/about`}>{t("footer.about")}</Link>
        </nav>
      </div>
    </footer>
  );
}
