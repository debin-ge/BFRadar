import { Link } from "react-router";

import type { Route } from "./+types/vendors";
import { internal } from "~/lib/api.server";
import { langOf } from "~/lib/i18n";
import { seoMeta } from "~/lib/seo";
import type { VendorsPage } from "~/lib/types";
import { useT } from "~/lib/use-t";

export async function loader({ request, url }: Route.LoaderArgs) {
  return internal<VendorsPage>(`/${langOf(url.pathname)}/vendors${url.search}`, request.signal);
}

export const meta: Route.MetaFunction = ({ loaderData }) => seoMeta(loaderData?.seo);

/** 商家目录（O-17）：本季有优惠、或近 30 天被社区讨论过的商家，每家一行；排序是两个链接，不用脚本。 */
export default function Vendors({ loaderData: d }: Route.ComponentProps) {
  const t = useT();
  return (
    <div className="board vendors-page">
      <h1 className="page-title">{t("vendors.heading")}</h1>
      <p className="lead">{t("vendors.lead")}</p>
      <div className="vendors-bar">
        <nav className="scopes" aria-label={t("vendors.sort")}>
          {d.sorts.map((s) => (
            <Link key={s.value} to={s.href} aria-current={s.current ? "page" : undefined}>
              {s.label}
            </Link>
          ))}
        </nav>
        <span className="total">{d.total}</span>
      </div>
      {d.rows.length > 0 ? (
        <div className="vd-wrap">
          <table className="vd">
            <thead>
              <tr>
                <th scope="col">{t("vendors.col.vendor")}</th>
                <th scope="col">{t("vendors.col.deals")}</th>
                <th scope="col">{t("vendors.col.low")}</th>
                <th scope="col">{t("vendors.col.latest")}</th>
                <th scope="col">{t("vendors.col.regions")}</th>
              </tr>
            </thead>
            <tbody>
              {d.rows.map((r) => (
                <tr key={r.href}>
                  <td data-c="vendor">
                    <Link to={r.href}>{r.name}</Link>
                    {r.aliases && <small>{r.aliases}</small>}
                  </td>
                  <td data-c="deals" data-l={t("vendors.col.deals")} className={r.has_deals ? undefined : "none"}>
                    {r.deals}
                  </td>
                  <td data-c="low" data-l={t("vendors.col.low")}>
                    {r.low}
                  </td>
                  <td data-c="latest" data-l={t("vendors.col.latest")}>
                    {r.latest}
                  </td>
                  <td data-c="regions">{r.regions}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        <p className="empty">{d.empty}</p>
      )}
    </div>
  );
}
