import { Link } from "react-router";

import { Amount, VendorName } from "~/components/common";
import { CopyCode } from "~/components/copy-code";
import type { T } from "~/lib/i18n";
import type { DealCard as Deal, PlanCol, PlanRow } from "~/lib/types";

const HEADINGS: Record<PlanCol, string> = {
  name: "plan.name",
  cpu: "plan.cpu",
  ram: "plan.ram",
  disk: "plan.disk",
  bw: "plan.bandwidth",
  port: "plan.port",
  loc: "plan.location",
  price: "plan.price",
  cny: "plan.cny",
  order: "plan.order",
};

function Cell({ p, col, t }: { p: PlanRow; col: PlanCol; t: T }) {
  switch (col) {
    case "name":
      return (
        <td data-c="name">
          {p.href ? <Link to={p.href}>{p.name || t("row.detail")}</Link> : p.name}
          {p.ip && <small>{p.ip}</small>}
          {p.net.map((x) => (
            <small key={x} className="net">
              {x}
            </small>
          ))}
        </td>
      );
    case "price":
      return (
        <td data-c="price">
          <b>{p.price}</b>
          {p.term && <small className="term">{p.term}</small>}
          {p.code && <CopyCode code={p.code} t={t} />}
        </td>
      );
    case "order":
      return (
        <td data-c="order">
          {p.order && (
            <a href={p.order} rel="nofollow noopener" target="_blank">
              {t("plan.order_link")}
            </a>
          )}
        </td>
      );
    case "bw":
    case "port":
      // 折成小卡片时"1 TB"分不清是流量还是存储：这两格带字段名（只在窄屏显示）
      return (
        <td data-c={col} data-l={t(HEADINGS[col])}>
          {p[col]}
        </td>
      );
    default:
      return <td data-c={col}>{p[col]}</td>;
  }
}

/** 套餐表：只画后端给的列（这批套餐里全空的列不画）。单元格只带 data-c，样式都挂在 .pt 上（这张表每页可能重复
 *  几十次，标记越少越好）；700 px 以下每行折成两行的小卡片：套餐名与价格一行，其余配置一行（见 app.css）。 */
export function PlanTable({ plans, cols, t }: { plans: PlanRow[]; cols: PlanCol[]; t: T }) {
  return (
    <div className="pt-wrap">
      <table className="pt">
        <thead>
          <tr>
            {cols.map((c) => (
              <th key={c} scope="col">
                {t(HEADINGS[c])}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {plans.map((p, i) => (
            <tr key={i}>
              {cols.map((c) => (
                <Cell key={c} p={p} col={c} t={t} />
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

/** 优惠卡片：起价是视觉中心，其余信息是小字。标题与"详情"都进入本站详情页。 */
export function DealCard({ d, t }: { d: Deal; t: T }) {
  return (
    <li className={d.expiry?.state === "ended" ? "deal is-ended" : "deal"} id={`d${d.id}`}>
      <p className="deal-meta">
        <VendorName v={d} />
        <span className={d.bf ? "ev ev-bf" : "ev"}>{d.event}</span>
      </p>
      <h3 className="deal-title">
        <Link to={d.href}>{d.title}</Link>
        {d.flags.map((f) => (
          <span key={f.key} className="flag" data-flag={f.key}>
            {f.label}
          </span>
        ))}
      </h3>
      <p className="deal-price">
        {d.price ? (
          <>
            <strong>
              <Amount text={d.price[0]} />
            </strong>
            {d.price[1] && <span>{d.price[1]}</span>}
          </>
        ) : (
          <span>{t("row.price_unknown")}</span>
        )}
      </p>
      {(d.offer.length > 0 || d.codes.length > 0 || d.specs.length > 0 || d.net.length > 0) && (
        <ul className="chips">
          {d.offer.map((x) => (
            <li key={x.kind} data-k={x.kind}>
              {x.text}
            </li>
          ))}
          {d.codes.map((c) => (
            <li key={c} data-k="code">
              <CopyCode code={c} t={t} />
            </li>
          ))}
          {d.codes_more && <li data-k="code-more">{d.codes_more}</li>}
          {d.net.map((x) => (
            <li key={x} data-k="net">
              {x}
            </li>
          ))}
          {d.specs.map((x, i) => (
            <li key={`s${i}`}>{x}</li>
          ))}
        </ul>
      )}
      {/* 底栏（发布时间、同一活动、详情）在宽屏上叠在"展开"那一行的右侧，卡片少一行（优化方案 O-11）；窄屏照旧单独一行 */}
      <div className={d.expand ? "deal-bottom" : "deal-bottom solo"}>
        <p className="deal-foot">
          <time dateTime={d.published_at ?? undefined}>{d.date}</time>
          {d.expiry && (
            <span className="expiry" data-state={d.expiry.state}>
              {d.expiry.text}
            </span>
          )}
          {d.same && d.same_href && (
            <Link className="same" to={d.same_href}>
              {d.same}
            </Link>
          )}
          <Link className="detail-link" to={d.href}>
            {t("row.detail")}
          </Link>
        </p>
        {d.expand && (
          <details className="more">
            <summary>{d.expand}</summary>
            {d.plans.length > 0 && <PlanTable plans={d.plans} cols={d.cols} t={t} />}
            {d.plans_more && <p className="plans-more">{d.plans_more}</p>}
            {d.excerpt && (
              <blockquote className="excerpt">
                <p>{d.excerpt}</p>
              </blockquote>
            )}
          </details>
        )}
      </div>
    </li>
  );
}
