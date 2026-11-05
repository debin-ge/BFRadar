import type { Config } from "@react-router/dev/config";

export default {
  ssr: true,
  // 首屏就带上全部路由清单：按需发现会让链接很多的页面向源站多发一串 /__manifest 请求
  routeDiscovery: { mode: "initial" },
} satisfies Config;
