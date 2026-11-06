import { Form, Link, useRouteLoaderData, useSubmit } from "react-router";

import type { Route } from "./+types/home";
import { filterHandlers, IntelList, Pager, ScopeTabs } from "~/components/common";
import { CountdownBlock } from "~/components/countdown";
import { DealCard } from "~/components/deal-card";
import { SponsorBanner } from "~/components/sponsor";
import { Button } from "~/components/ui/button";
import { Input } from "~/components/ui/input";
import { NativeSelect, NativeSelectOption } from "~/components/ui/native-select";
import { internal } from "~/lib/api.server";
import { langOf, type T } from "~/lib/i18n";
import { seoMeta } from "~/lib/seo";
import type { Chrome, Home } from "~/lib/types";
import { useT } from "~/lib/use-t";

export async function loader({ request, url }: Route.LoaderArgs) {
  return internal<Home>(`/${langOf(url.pathname)}/home${url.search}`, request.signal);
}

export const meta: Route.MetaFunction = ({ loaderData }) => seoMeta(loaderData?.seo);

const STATS = [
  ["bf", "stats.bf"],
  ["new", "stats.new"],
  ["plans", "stats.plans"],
  ["vendors", "stats.vendors"],
] as const;

/** 雷达屏（纯装饰）：刚刚上架的每条优惠是一个光点，位置按序号用黄金角铺开，服务端与浏览器算出同一结果。
 *  CSP 不许内联样式，光点的闪烁错开由 CSS 按序号给。 */
function Radar({ count }: { count: number }) {
  const blips = Array.from({ length: Math.min(count, 8) }, (_, i) => {
    const angle = ((i * 137.5 + 30) * Math.PI) / 180;
    const r = 26 + ((i * 0.618) % 1) * 62;
    return [(100 + r * Math.cos(angle)).toFixed(1), (100 + r * Math.sin(angle)).toFixed(1)] as const;
  });
  return (
    <div className="radar" aria-hidden="true">
      <svg viewBox="0 0 200 200">
        {[96, 78, 60, 42, 24].map((r) => (
          <circle key={r} className="ring" cx="100" cy="100" r={r} />
        ))}
        <path className="axis" d="M100 4v192M4 100h192" />
        <g className="blips">
          {blips.map(([x, y], i) => (
            <circle key={i} cx={x} cy={y} r="1.5" />
          ))}
        </g>
      </svg>
      <div className="radar-sweep" />
    </div>
  );
}

/** "24 小时动态"（优化方案 O-10）：右侧栏顶上，不受筛选影响。一行一条：什么动静、多久以前、商家、起价。 */
function JustListed({ tags, t }: { tags: Home["just"]; t: T }) {
  if (tags.length === 0) return null;
  return (
    <section className="shelf" aria-labelledby="shelf-title">
      <h2 id="shelf-title">{t("just.title")}</h2>
      <ol className="tags">
        {tags.map((x) => (
          <li key={x.id}>
            <Link className="tag" to={x.href}>
              <span className="tag-kind" data-kind={x.kind}>
                {x.kind_label}
              </span>
              <b className="tag-vendor">{x.vendor}</b>
              <span className="tag-price">{x.price ?? t("row.price_unknown")}</span>
              <time className="tag-ago" dateTime={x.at ?? undefined}>
                {x.ago}
              </time>
              <span className="tag-cny">{x.cny}</span>
            </Link>
          </li>
        ))}
      </ol>
    </section>
  );
}

/** 筛选：原生 GET 表单，没有脚本照常提交；水合后勾选或换排序即提交，"筛选"按钮收起（搜索框回车提交）。 */
function FilterBar({ form, total, t }: { form: Home["form"]; total: string; t: T }) {
  const submit = useSubmit();
  return (
    <Form method="get" action={form.action} className="filters" {...filterHandlers(submit)}>
      {form.scope && <input type="hidden" name="scope" value={form.scope} />}
      <fieldset className="filter-products">
        <legend>{t("filter.product")}</legend>
        {form.products.map((p) => (
          <label key={p.value} className="check">
            <input type="checkbox" name="product" value={p.value} defaultChecked={p.checked} />
            {t(`product.${p.value}`)}
          </label>
        ))}
      </fieldset>
      <div className="filter-row">
        <Input
          className="w-full tab:w-60"
          type="search"
          name="q"
          defaultValue={form.q}
          placeholder={t("filter.search")}
          maxLength={100}
          aria-label={t("filter.search")}
        />
        <label className="filter-sort">
          {t("filter.sort")}
          <NativeSelect name="sort" defaultValue={form.sorts.find((s) => s.selected)?.value}>
            {form.sorts.map((s) => (
              <NativeSelectOption key={s.value} value={s.value}>
                {t(`sort.${s.value}`)}
              </NativeSelectOption>
            ))}
          </NativeSelect>
        </label>
        {form.ended !== null && (
          <label className="check">
            <input type="checkbox" name="ended" value="1" defaultChecked={form.ended} />
            {t("filter.ended")}
          </label>
        )}
        <Button type="submit" className="filter-submit">
          {t("filter.submit")}
        </Button>
        {form.reset && (
          <Link className="reset" to={form.reset}>
            {t("filter.reset")}
          </Link>
        )}
        <span className="total">{total}</span>
      </div>
    </Form>
  );
}

export default function HomePage({ loaderData: d }: Route.ComponentProps) {
  const t = useT();
  const scope = d.scopes.find((s) => s.current)?.key ?? "bf";
  const chrome = useRouteLoaderData<{ chrome?: Chrome }>("root")?.chrome;
  return (
    <>
      {chrome && (
        <CountdownBlock countdown={chrome.countdown} t={t} aside={<Radar count={d.just.length} />}>
          <dl className="stats">
            {STATS.map(([key, label]) => (
              <div key={key}>
                <dt>{t(label)}</dt>
                <dd>{d.stats[key]}</dd>
              </div>
            ))}
          </dl>
        </CountdownBlock>
      )}
      <SponsorBanner t={t} />
      <div className="board-layout">
        <section className="board" aria-label={t(`scope.${scope}`)}>
          <ScopeTabs scopes={d.scopes} t={t} />
          <FilterBar key={d.seo.canonical} form={d.form} total={d.total} t={t} />
          {d.deals.length > 0 ? (
            <ol className="deals">
              {d.deals.map((deal) => (
                <DealCard key={deal.id} d={deal} t={t} />
              ))}
            </ol>
          ) : (
            <p className="empty">{d.empty}</p>
          )}
          {d.more && (
            <p className="more-scope">
              <Link to={d.more.href}>{d.more.text}</Link>
            </p>
          )}
          <Pager pager={d.pager} t={t} />
        </section>
        <aside className="rail" aria-label={t("community.title")}>
          <JustListed tags={d.just} t={t} />
          <section aria-labelledby="community-title">
            <h2 id="community-title">{t("community.title")}</h2>
            {d.intel.length > 0 ? (
              <IntelList items={d.intel} />
            ) : (
              <p className="empty">{t("community.empty")}</p>
            )}
          </section>
        </aside>
      </div>
    </>
  );
}
