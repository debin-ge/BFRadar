import { createContext, useContext } from "react";

/** 每个请求的 CSP nonce：entry.server 生成并放进这个 context；浏览器端拿不到，也不需要。 */
export const NonceContext = createContext<string | undefined>(undefined);

export function useNonce() {
  return useContext(NonceContext);
}
