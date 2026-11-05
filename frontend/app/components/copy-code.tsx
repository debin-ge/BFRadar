import { useEffect, useState } from "react";

import type { T } from "~/lib/i18n";

/** 优惠码（优化方案 O-07）：码本身点一下就全选（没有脚本时照样能复制）；水合后旁边出现复制按钮，复制成功显示对勾，
 *  读屏软件读出"已复制"。图标由 CSS 画（.copy），每个码只多一个空按钮，页面上码多也不怎么涨体积。 */
export function CopyCode({ code, t, prefix = true }: { code: string; t: T; prefix?: boolean }) {
  const [copied, setCopied] = useState(false);
  useEffect(() => {
    if (!copied) return;
    const timer = setTimeout(() => setCopied(false), 1500);
    return () => clearTimeout(timer);
  }, [copied]);

  async function copy() {
    try {
      await navigator.clipboard.writeText(code);
      setCopied(true);
    } catch {
      // 剪贴板接口不可用（非 HTTPS、权限被拒）：码本身仍可点选复制
    }
  }

  return (
    <span className="code" aria-live="polite">
      {prefix && <span className="code-prefix">{t("code.prefix")}</span>}
      <code>{code}</code>
      <button
        type="button"
        className="copy"
        data-copied={copied ? "" : undefined}
        onClick={copy}
        aria-label={t("code.copy", { code })}
        title={t("code.copy", { code })}
      />
      {copied && <span className="sr-only">{t("code.copied")}</span>}
    </span>
  );
}
