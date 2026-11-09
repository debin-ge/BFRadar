import { data, Link } from "react-router";

import type { Route } from "./+types/deal";
import { Amount, IntelList, VendorName } from "~/components/common";
import { CopyCode } from "~/components/copy-code";
import { PlanTable } from "~/components/deal-card";
import { Button } from "~/components/ui/button";
import { internal } from "~/lib/api.server";
import { langOf } from "~/lib/i18n";
import { seoMeta } from "~/lib/seo";
import type { DealPage } from "~/lib/types";
import { useT } from "~/lib/use-t";

export async function loader({ request, url, params }: Route.LoaderArgs) {
  // 编号只认数字（/deal/abc 直接 404，不去问后端）
  if (!/^\d{1,18}$/.test(params.id)) throw data(null, { status: 404 });
  return internal<DealPage>(`/${langOf(url.pathname)}/deal/${params.id}`, request.signal);
}

export const meta: Route.MetaFunction = ({ loaderData }) => seoMeta(loaderData?.seo);

/** 优惠详情（FR-10）：标题卡先给摘要和商家下单入口，再是字段卡片、全部套餐，侧栏放时间线与相关内容。 */
export default function DealDetail({ loaderData: d }: Route.ComponentProps) {
  const t = useT();
  const f = d.facts;
  // 侧栏没内容（无变更、无同一活动的其他帖子、无同商家活动、无讨论）时套餐表占满整行
  const side = d.changes.length > 0 || d.same.length > 0 || d.others.length > 0 || d.intel.length > 0;
  return (
    <article className="detail">
      <header className="panel hero has-price">
        <div className="hero-main">
          <p className="deal-meta">
            <VendorName v={d} />
            {d.bf && <span className="ev ev-bf">{d.event}</span>}
          </p>
          <h1 className="page-title">{d.title}</h1>
          {d.original_title && <p className="original-title">{d.original_title}</p>}
          {d.summary && (
            <p className="summary">
              {d.summary}
              <span className="note">{t("deal.auto_summary")}</span>
            </p>
          )}
          {d.purchase_url && (
            <p className="actions">
              <Button asChild size="lg" variant="outline" className="bg-card">
                <a href={d.purchase_url} rel="nofollow noopener" target="_blank">
                  {t("deal.buy")}
                </a>
              </Button>
            </p>
          )}
        </div>
        <dl className="hero-price">
          <dt>{t("deal.start")}</dt>
          <dd>
            {f.price ? (
              <>
                <strong>
                  <Amount text={f.price[0]} />
                </strong>
                {f.price[1] && <span>{f.price[1]}</span>}
              </>
            ) : (
              t("row.price_unknown")
            )}
          </dd>
          {f.expiry && (
            <>
              <dt className="hero-valid-label">{t("deal.validity")}</dt>
              <dd className="expiry" data-state={f.expiry.state}>
                {f.expiry.text}
              </dd>
            </>
          )}
          {f.codes.length > 0 && (
            <>
              <dt className="hero-codes-label">{t("deal.codes")}</dt>
              <dd className="hero-codes">
                {f.codes.map((c) => (
                  <CopyCode key={c} code={c} t={t} prefix={false} />
                ))}
              </dd>
            </>
          )}
        </dl>
      </header>

      <dl className="facts">
        <div>
          <dt>{t("deal.event")}</dt>
          <dd>{d.event}</dd>
        </div>
        <div>
          <dt>{t("deal.products")}</dt>
          <dd>{f.products}</dd>
        </div>
        <div>
          <dt>{t("deal.discount")}</dt>
          <dd>
            {f.discount}
            {f.discount_note && <span className="note">{f.discount_note}</span>}
          </dd>
        </div>
        <div>
          <dt>{t("deal.recurring")}</dt>
          <dd>{f.recurring}</dd>
        </div>
        <div>
          <dt>{t("deal.published")}</dt>
          <dd>{f.published}</dd>
        </div>
        <div>
          <dt>{t("deal.seen")}</dt>
          <dd>{f.seen}</dd>
        </div>
      </dl>

      <div className={side ? "detail-layout has-side" : "detail-layout"}>
        <div className="detail-main">
          <section className="panel">
            <h2>{d.plans_heading}</h2>
            {d.plans.length > 0 ? <PlanTable plans={d.plans} cols={d.cols} t={t} /> : <p className="empty">{t("deal.no_plans")}</p>}
          </section>
          {d.excerpt && (
            <section className="panel">
              <h2>{t("deal.excerpt")}</h2>
              <blockquote className="excerpt">
                <p>{d.excerpt}</p>
              </blockquote>
            </section>
          )}
        </div>
        {side && (
          <aside className="detail-side">
            {d.changes.length > 0 && (
              <section className="panel">
                <h2>{t("deal.timeline")}</h2>
                <ol className="timeline">
                  {d.changes.map((c, i) => (
                    <li key={i}>
                      <time dateTime={c.at}>{c.time}</time>
                      <span>{c.text}</span>
                    </li>
                  ))}
                </ol>
              </section>
            )}
            {d.same_heading && (
              <section className="panel" id="same">
                <h2>{d.same_heading}</h2>
                <ul className="links">
                  {d.same.map((o) => (
                    <li key={o.href}>
                      <Link to={o.href}>{o.title}</Link>
                      <span>{o.meta}</span>
                    </li>
                  ))}
                </ul>
              </section>
            )}
            {d.others_heading && (
              <section className="panel">
                <h2>{d.others_heading}</h2>
                <ul className="links">
                  {d.others.map((o) => (
                    <li key={o.href}>
                      <Link to={o.href}>{o.title}</Link>
                      <span>{o.meta}</span>
                    </li>
                  ))}
                </ul>
              </section>
            )}
            {d.intel.length > 0 && (
              <section className="panel">
                <h2>{t("deal.intel")}</h2>
                <IntelList items={d.intel} />
              </section>
            )}
          </aside>
        )}
      </div>
    </article>
  );
}
