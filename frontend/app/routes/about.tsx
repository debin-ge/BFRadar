import type { Route } from "./+types/about";
import { internal } from "~/lib/api.server";
import { langOf } from "~/lib/i18n";
import { seoMeta } from "~/lib/seo";
import type { AboutPage } from "~/lib/types";
import { useT } from "~/lib/use-t";

export async function loader({ request, url }: Route.LoaderArgs) {
  return internal<AboutPage>(`/${langOf(url.pathname)}/about`, request.signal);
}

export const meta: Route.MetaFunction = ({ loaderData }) => seoMeta(loaderData?.seo);

/** 关于与免责（FR-20、第 17 节）。带数据的只有本季起点，由后端给出；没有"数据来源"和"更新频率"两节（2026-11-05 按用户要求去掉）。
 *  版式按排好的 Markdown 文档来：真正的标题层级、圆点列表、有线框的表格（D69）；外面套一层 .doc 让版心规则管边距。 */
export default function About({ loaderData: d }: Route.ComponentProps) {
  const t = useT();
  return (
    <div className="doc">
      <article className="prose">
        <h1 className="page-title">{t("about.heading")}</h1>
        <p className="lead">{t("about.lead")}</p>

        <h2 id="scope">{t("about.scope.title")}</h2>
        <dl className="terms">
          <dt>{t("scope.bf")}</dt>
          <dd>{d.scope_bf}</dd>
          <dt>{t("scope.promo")}</dt>
          <dd>{t("about.scope.promo")}</dd>
          <dt>{t("scope.all")}</dt>
          <dd>{t("about.scope.all")}</dd>
        </dl>

        <h2 id="prices">{t("about.price.title")}</h2>
        <p>{t("about.price.original")}</p>
        <p>{t("about.price.cny")}</p>
        <p>{t("about.price.note")}</p>

        <h2 id="disclaimer">{t("about.disclaimer.title")}</h2>
        <ul>
          <li>{t("about.disclaimer.price")}</li>
          <li>{t("about.disclaimer.advice")}</li>
          <li>{t("about.disclaimer.auto")}</li>
          <li>{t("about.disclaimer.links")}</li>
        </ul>

        <h2 id="sponsor">{t("about.sponsor.title")}</h2>
        <p>{t("about.sponsor.text")}</p>

        <h2 id="privacy">{t("about.privacy.title")}</h2>
        <p>{t("about.privacy.text")}</p>
      </article>
    </div>
  );
}
