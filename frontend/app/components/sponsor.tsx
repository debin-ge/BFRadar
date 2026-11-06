import type { T } from "~/lib/i18n";
import { SPONSOR_URL } from "~/lib/sponsor";

/** 赞助链接一律 sponsored（付费或推广关系的标准标注）+ nofollow，与站内其他外链同一套写法。 */
export const SPONSOR_REL = "sponsored nofollow noopener";

/** 标题里把赞助方的名字单独拎出来上渐变色（找不到就原样输出，换语言不会坏）。 */
function Title({ t }: { t: T }) {
  const title = t("sponsor.title");
  const name = t("sponsor.name");
  const at = title.indexOf(name);
  if (at < 0) return <>{title}</>;
  return (
    <>
      {title.slice(0, at)}
      <span className="sb-hl">{name}</span>
      {title.slice(at + name.length)}
    </>
  );
}

/** 首页倒计时下面的赞助横幅：配色取自 tiktoken.net 的主题（深石板蓝黑底、靛紫到青的渐变）。左边品牌与一句话，
 *  右边斜切的渐变块放赠送额度。整块可点（链接的伪元素铺满横幅），"赞助"标明性质。外层 aside 吃页面版心的边距，
 *  里层 .sponsor-banner 才是画出来的横幅。 */
export function SponsorBanner({ t }: { t: T }) {
  return (
    <aside className="sponsor-slot" aria-label={t("sponsor.label")}>
      <div className="sponsor-banner">
        <div className="sb-main">
          <div className="sb-brand">
            <span className="sb-logo">
              <img src="/tiktoken-logo.png" alt="TikToken" width={360} height={92} decoding="async" />
            </span>
            <span className="sb-tag">{t("sponsor.label")}</span>
          </div>
          <div className="sb-text">
            <p className="sb-title">
              <Title t={t} />
            </p>
            <p className="sb-sub">{t("sponsor.sub")}</p>
            <p className="sb-action">
              <a className="sb-cta" href={SPONSOR_URL} rel={SPONSOR_REL} target="_blank">
                {t("sponsor.cta")} →
              </a>
              <span className="sb-domain">tiktoken.net</span>
            </p>
          </div>
        </div>
        <div className="sb-offer" aria-hidden="true">
          <span className="sb-offer-label">{t("sponsor.offer.label")}</span>
          <b className="sb-offer-amount">$3–10</b>
          <span className="sb-offer-note">{t("sponsor.offer.note")}</span>
        </div>
      </div>
    </aside>
  );
}

/** 页脚里的一句（所有页面）。 */
export function SponsorFootnote({ t }: { t: T }) {
  return (
    <p className="sponsor-foot">
      <a href={SPONSOR_URL} rel={SPONSOR_REL} target="_blank">
        {t("sponsor.footer")}
      </a>
    </p>
  );
}
