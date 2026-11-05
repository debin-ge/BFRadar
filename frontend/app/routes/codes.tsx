import { Link } from "react-router";

import type { Route } from "./+types/codes";
import { CopyCode } from "~/components/copy-code";
import { internal } from "~/lib/api.server";
import { langOf } from "~/lib/i18n";
import { seoMeta } from "~/lib/seo";
import type { CodesPage } from "~/lib/types";
import { useT } from "~/lib/use-t";

export async function loader({ request, url }: Route.LoaderArgs) {
  return internal<CodesPage>(`/${langOf(url.pathname)}/codes`, request.signal);
}

export const meta: Route.MetaFunction = ({ loaderData }) => seoMeta(loaderData?.seo);

/** 优惠码汇总（O-18）：本季带码的优惠一行一条，码点一下就能复制，链到详情页；已结束的整行压暗、排在最后。 */
export default function Codes({ loaderData: d }: Route.ComponentProps) {
  const t = useT();
  return (
    <div className="board codes-page">
      <h1 className="page-title">{t("codes.heading")}</h1>
      <p className="lead">{t("codes.lead")}</p>
      <p className="total">{d.total}</p>
      {d.rows.length > 0 ? (
        <div className="vd-wrap">
          <table className="vd cd">
            <thead>
              <tr>
                <th scope="col">{t("codes.col.vendor")}</th>
                <th scope="col">{t("codes.col.codes")}</th>
                <th scope="col">{t("codes.col.discount")}</th>
                <th scope="col">{t("codes.col.recurring")}</th>
                <th scope="col">{t("codes.col.expiry")}</th>
                <th scope="col">
                  <span className="sr-only">{t("row.detail")}</span>
                </th>
              </tr>
            </thead>
            <tbody>
              {d.rows.map((r) => (
                <tr key={r.id} className={r.expiry?.state === "ended" ? "is-ended" : undefined}>
                  <td data-c="vendor">
                    {r.vendor_href ? <Link to={r.vendor_href}>{r.vendor}</Link> : <b>{r.vendor}</b>}
                    <small>{r.date}</small>
                  </td>
                  <td data-c="codes">
                    <div className="code-list">
                      {r.codes.map((c) => (
                        <CopyCode key={c} code={c} t={t} prefix={false} />
                      ))}
                    </div>
                  </td>
                  <td data-c="discount" data-l={t("codes.col.discount")}>
                    {r.discount}
                  </td>
                  <td data-c="recurring" data-l={t("codes.col.recurring")}>
                    {r.recurring}
                  </td>
                  <td data-c="expiry" data-l={t("codes.col.expiry")}>
                    {r.expiry && (
                      <span className="expiry" data-state={r.expiry.state}>
                        {r.expiry.text}
                      </span>
                    )}
                  </td>
                  <td data-c="detail">
                    <Link className="detail-link" to={r.href} title={r.title}>
                      {t("row.detail")}
                    </Link>
                  </td>
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
