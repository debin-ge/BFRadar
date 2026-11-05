import type { Route } from "./+types/status";
import { internal } from "~/lib/api.server";
import { langOf } from "~/lib/i18n";
import { seoMeta } from "~/lib/seo";
import type { StatusPage } from "~/lib/types";
import { useT } from "~/lib/use-t";

export async function loader({ request, url }: Route.LoaderArgs) {
  return internal<StatusPage>(`/${langOf(url.pathname)}/status`, request.signal);
}

export const meta: Route.MetaFunction = ({ loaderData }) => seoMeta(loaderData?.seo);

/** 采集状态（FR-13）：维护者看的页面，不在任何页面上链接（D59），不缓存；任一源失败在这里可见。 */
export default function Status({ loaderData: d }: Route.ComponentProps) {
  const t = useT();
  return (
    <article className="detail status-page">
      <header className="panel hero">
        <h1 className="page-title">{t("statuspage.heading")}</h1>
        <p className="lead">{t("statuspage.lead")}</p>
      </header>
      <dl className="facts">
        {d.facts.map((f) => (
          <div key={f.label}>
            <dt>{f.label}</dt>
            <dd>
              {f.value}
              {f.note && <span className="note">{f.note}</span>}
            </dd>
          </div>
        ))}
      </dl>
      {d.sources.map((s) => (
        <section className="panel" id={s.key} key={s.key}>
          <h2>{s.name}</h2>
          <p className="run-meta">
            {s.meta.map((m) => (
              <span key={m}>{m}</span>
            ))}
            {s.failing && <strong className="failing">{s.failing}</strong>}
          </p>
          {s.rows.length > 0 ? (
            <div className="pt-wrap">
              <table className="pt runs">
                <thead>
                  <tr>
                    {s.columns.map((c) => (
                      <th scope="col" key={c}>
                        {c}
                      </th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {s.rows.map((r) => (
                    <tr className={r.bad ? "bad" : undefined} key={r.started_at}>
                      <td>
                        <time dateTime={r.started_at}>{r.time}</time>
                      </td>
                      <td>{r.took}</td>
                      {r.cells.map((c, i) => (
                        <td key={i}>{c}</td>
                      ))}
                      <td className="result">{r.result}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ) : (
            <p className="empty">{s.empty}</p>
          )}
        </section>
      ))}
    </article>
  );
}
