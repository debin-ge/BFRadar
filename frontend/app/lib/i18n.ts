// 界面字典与 RSS 共用后端的 backend/app/i18n（键一一对应，测试核对）。带数据的文字由后端格式化好给出，
// 这里只取不带数据的固定文字，以及倒计时这类要在浏览器里刷新的模板。
import en from "../../../backend/app/i18n/en.json";
import zh from "../../../backend/app/i18n/zh-CN.json";

export type Lang = "zh" | "en";
export type T = (key: string, params?: Record<string, string | number>) => string;

const TABLES: Record<Lang, Record<string, string>> = { zh, en };

export const HTML_LANG: Record<Lang, string> = { zh: "zh-CN", en: "en" };
export const PREFIX: Record<Lang, string> = { zh: "", en: "/en" };

/** 中文在根路径，英文在 /en/ 下（第 7 节）。 */
export function langOf(pathname: string): Lang {
  return pathname === "/en" || pathname.startsWith("/en/") ? "en" : "zh";
}

function translator(lang: Lang): T {
  const table = TABLES[lang];
  return (key, params) => {
    const text = table[key];
    if (text === undefined) {
      // 缺词条不让整页报错：页面上露出键名，控制台提醒（与 Python 版相同）
      console.warn(`字典 ${lang} 缺少 ${key}`);
      return key;
    }
    return params ? text.replace(/\{(\w+)\}/g, (all, name: string) => (name in params ? String(params[name]) : all)) : text;
  };
}

/** 每种语言一个固定的 t()，函数身份稳定，可以放进 effect 的依赖。 */
export const TRANSLATORS: Record<Lang, T> = { zh: translator("zh"), en: translator("en") };
