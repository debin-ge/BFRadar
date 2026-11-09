import { Link, type SubmitFunction } from "react-router";

import type { T } from "~/lib/i18n";
import type { IntelItem, Pager as PagerData, VendorLink } from "~/lib/types";

/** 范围三档（D31）：链接，不是标签页控件，没有脚本也能切换。 */
export function ScopeTabs({ scopes, t }: { scopes: { key: string; href: string; current: boolean }[]; t: T }) {
  return (
    <nav className="scopes">
      {scopes.map((s) => (
        <Link key={s.key} to={s.href} aria-current={s.current ? "page" : undefined}>
          {t(`scope.${s.key}`)}
        </Link>
      ))}
    </nav>
  );
}

export function Pager({ pager, t }: { pager: PagerData; t: T }) {
  if (!pager) return null;
  return (
    <nav className="pager">
      {pager.prev && (
        <Link to={pager.prev} rel="prev">
          {t("page.prev")}
        </Link>
      )}
      <span className="pager-text">{pager.text}</span>
      <span className="pager-pages">
        {pager.pages.map((p, i) =>
          p ? (
            <Link key={p.n} to={p.href} aria-current={p.current ? "page" : undefined}>
              {p.n}
            </Link>
          ) : (
            <span key={`gap${i}`} aria-hidden="true">
              …
            </span>
          ),
        )}
      </span>
      {pager.next && (
        <Link to={pager.next} rel="next">
          {t("page.next")}
        </Link>
      )}
    </nav>
  );
}

/** 社区讨论：只展示标题与相对时间，不提供出处和外链。 */
export function IntelList({ items }: { items: IntelItem[] }) {
  return (
    <ul className="intel">
      {items.map((x, i) => (
        <li key={i}>
          {x.title}
          <span>{x.ago}</span>
        </li>
      ))}
    </ul>
  );
}

/** 商家名，后面跟最近 7 天的讨论热度（链到商家页的讨论列表）。 */
/** "$68.20/月" 拆成金额与周期，周期用小字（D68）；没有"/"（"免费"）时原样。文字拼起来与原来一致。 */
export function Amount({ text }: { text: string }) {
  const i = text.indexOf("/");
  if (i < 0) return <>{text}</>;
  return (
    <>
      {text.slice(0, i)}
      <small>{text.slice(i)}</small>
    </>
  );
}

export function VendorName({ v }: { v: VendorLink }) {
  return (
    <>
      {v.vendor_href ? (
        <Link className="vendor" to={v.vendor_href}>
          {v.vendor}
        </Link>
      ) : (
        <span className="vendor">{v.vendor}</span>
      )}
      {v.heat && v.vendor_href && (
        <Link className="heat" to={`${v.vendor_href}#intel`} title={v.heat_title ?? undefined}>
          {v.heat}
        </Link>
      )}
    </>
  );
}

/** 有脚本时提交筛选：去掉空值（"不限"的下拉、空搜索框），地址干净可分享。没有脚本时表单照常整表提交，后端会规范掉。 */
export function submitClean(form: HTMLFormElement, submit: SubmitFunction, replace = false) {
  const params = new URLSearchParams();
  for (const [name, value] of new FormData(form)) {
    if (typeof value === "string" && value !== "") params.append(name, value);
  }
  submit(params, { method: "get", action: form.getAttribute("action") ?? undefined, preventScrollReset: true, replace });
}

/** 勾选框与下拉一改就提交（替换当前历史记录）；文本框等回车。 */
export function filterHandlers(submit: SubmitFunction) {
  return {
    onChange(event: React.FormEvent<HTMLFormElement>) {
      if ((event.target as HTMLElement).matches("input[type=checkbox], select")) {
        submitClean(event.currentTarget, submit, true);
      }
    },
    onSubmit(event: React.FormEvent<HTMLFormElement>) {
      event.preventDefault();
      submitClean(event.currentTarget, submit);
    },
  };
}
