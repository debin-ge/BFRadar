import type { MetaDescriptor } from "react-router";

import type { Seo } from "./types";

/** 每页的 meta（M3.4）：title、description、canonical、hreflang（zh-CN、en、x-default 指中文）、Open Graph、JSON-LD。
 *  内容全由后端 seo.py 给出（与 Jinja 页面同一份），地址只按 SITE_URL 拼。JSON-LD 由 React Router 转义后写进
 *  <script type="application/ld+json">，不执行，不受 CSP 限制。 */
export function seoMeta(seo: Seo | undefined): MetaDescriptor[] {
  if (!seo) return [];
  const og = seo.og;
  return [
    { title: seo.title },
    { name: "description", content: seo.description },
    { tagName: "link", rel: "canonical", href: seo.canonical },
    { tagName: "link", rel: "alternate", hrefLang: "zh-CN", href: seo.site_url + seo.alternates.zh },
    { tagName: "link", rel: "alternate", hrefLang: "en", href: seo.site_url + seo.alternates.en },
    { tagName: "link", rel: "alternate", hrefLang: "x-default", href: seo.site_url + seo.alternates.zh },
    { property: "og:type", content: og.type },
    { property: "og:title", content: og.title },
    { property: "og:description", content: og.description },
    { property: "og:url", content: og.url },
    { property: "og:site_name", content: og.site_name },
    { property: "og:locale", content: og.locale },
    ...(og.image
      ? [
          { property: "og:image", content: og.image },
          { property: "og:image:width", content: "1200" },
          { property: "og:image:height", content: "630" },
        ]
      : []),
    { name: "twitter:card", content: og.image ? "summary_large_image" : "summary" },
    ...seo.jsonld.map((item) => ({ "script:ld+json": item })),
  ];
}
