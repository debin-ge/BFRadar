import { Link } from "react-router";

import type { Route } from "./+types/vendor";
import { IntelList } from "~/components/common";
import { PlanTable } from "~/components/deal-card";
import { Button } from "~/components/ui/button";
import { internal } from "~/lib/api.server";
import { langOf } from "~/lib/i18n";
import { seoMeta } from "~/lib/seo";
import type { VendorPage } from "~/lib/types";
import { useT } from "~/lib/use-t";

export async function loader({ request, url, params }: Route.LoaderArgs) {
  return internal<VendorPage>(`/${langOf(url.pathname)}/vendor/${encodeURIComponent(params.slug)}`, request.signal);
}

export const meta: Route.MetaFunction = ({ loaderData }) => seoMeta(loaderData?.seo);

function DealLinks({ items }: { items: VendorPage["deals"] }) {
  return (
    <ul className="links">
      {items.map((x) => (
        <li key={x.href}>
          {x.event && <span className="ev ev-bf">{x.event}</span>}
          <Link to={x.href}>{x.title}</Link>
          <span>
            {x.meta}
            {x.expiry && (
              <>
                {" · "}
                <span className="expiry" data-state={x.expiry.state}>
                  {x.expiry.text}
                </span>
              </>
            )}
          </span>
        </li>
      ))}
    </ul>
  );
}

/** 商家页（FR-11、O-17）：别名、官网、机房与线路、历史最低起价；本季在售套餐（去重）、本季与往季的优惠；
 *  社区讨论（锚点 #intel，首页热度链到这里）。 */
export default function VendorDetail({ loaderData: d }: Route.ComponentProps) {
  const t = useT();
  return (
    <article className="detail">
      <header className={d.lowest ? "panel hero has-price" : "panel hero"}>
        <div className="hero-main">
          <h1 className="page-title">{d.name}</h1>
          {d.aka && <p className="original-title">{d.aka}</p>}
          {(d.places.length > 0 || d.net.length > 0) && (
            <dl className="vendor-facts">
              {d.places.length > 0 && (
                <>
                  <dt>{t("vendor.places")}</dt>
                  <dd>
                    {d.places.map((p) => (
                      <span key={p.region}>
                        <b>{p.region}</b> {p.cities}
                      </span>
                    ))}
                  </dd>
                </>
              )}
              {d.net.length > 0 && (
                <>
                  <dt>{t("vendor.net")}</dt>
                  <dd>
                    {d.net.map((x) => (
                      <small key={x} className="net">
                        {x}
                      </small>
                    ))}
                  </dd>
                </>
              )}
            </dl>
          )}
          {d.website && (
            <p className="actions">
              <Button asChild size="lg" variant="outline" className="bg-card">
                <a href={d.website} rel="nofollow noopener" target="_blank">
                  {t("vendor.website")}
                </a>
              </Button>
            </p>
          )}
        </div>
        {d.lowest && (
          <dl className="hero-price">
            <dt>{t("vendor.lowest")}</dt>
            <dd>
              <strong>{d.lowest.price[0]}</strong>
              {d.lowest.price[1] && <span>{d.lowest.price[1]}</span>}
            </dd>
            <dd className="hero-price-note">
              <Link to={d.lowest.href}>{d.lowest.title}</Link>
              <span>{d.lowest.date}</span>
            </dd>
          </dl>
        )}
      </header>

      <div className="detail-layout has-side">
        <div className="detail-main">
          <section className="panel">
            <h2>{d.plans_heading ?? t("vendor.no_plans")}</h2>
            {d.plans.length > 0 && <PlanTable plans={d.plans} cols={d.cols} t={t} />}
          </section>
          <section className="panel">
            <h2>{d.deals_heading}</h2>
            {d.deals.length > 0 ? <DealLinks items={d.deals} /> : <p className="empty">{t("vendor.no_deals")}</p>}
          </section>
          {d.past_heading && (
            <section className="panel">
              <h2>{d.past_heading}</h2>
              <DealLinks items={d.past} />
            </section>
          )}
        </div>
        <section className="panel detail-side" id="intel">
          <h2>{t("vendor.intel")}</h2>
          {d.intel.length > 0 ? <IntelList items={d.intel} /> : <p className="empty">{t("vendor.no_intel")}</p>}
        </section>
      </div>
    </article>
  );
}
