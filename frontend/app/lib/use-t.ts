import { useLocation } from "react-router";

import { langOf, TRANSLATORS } from "./i18n";

/** 当前页面语言的 t()：语言按地址前缀判断。 */
export function useT() {
  return TRANSLATORS[langOf(useLocation().pathname)];
}
