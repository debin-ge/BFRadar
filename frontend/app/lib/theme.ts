// 主题：跟随系统，或访客手动选浅色 / 深色。选择只存 localStorage、不用 cookie：带 Set-Cookie 的响应
// Cloudflare 不缓存，按 cookie 渲染又会让同一地址出两种 HTML。<head> 里的内联脚本在首帧前把选择写到 html[data-theme]，
// 没选过就不写，CSS 按 prefers-color-scheme 跟随系统。
//
// 同一段脚本还给 html 加 data-js：有脚本的访客从第一帧起就把"筛选"按钮收起（勾选即提交），
// 免得水合后再收起让下面的内容跳一下（CLS）。

export const PREPAINT_SCRIPT =
  'var d=document.documentElement;d.setAttribute("data-js","");' +
  'try{var t=localStorage.getItem("theme");if(t==="light"||t==="dark")d.setAttribute("data-theme",t)}catch(e){}';

type Choice = "light" | "dark" | null;

/** 跟随系统 → 浅色 → 深色 → 跟随系统。 */
export function cycleTheme() {
  const root = document.documentElement;
  const current = root.getAttribute("data-theme") as Choice;
  const next: Choice = current === null ? "light" : current === "light" ? "dark" : null;
  try {
    if (next) localStorage.setItem("theme", next);
    else localStorage.removeItem("theme");
  } catch {
    // 隐私模式等存不了时只在本页生效
  }
  if (next) root.setAttribute("data-theme", next);
  else root.removeAttribute("data-theme");
}
