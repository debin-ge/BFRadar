import { index, route, type RouteConfig } from "@react-router/dev/routes";

// 中文在根路径、英文在 /en/ 下，共用一棵路由树（可选段 "en?"）；语言一律按地址前缀判断，不看路由 id。
// 状态页 /status 不在任何页面上链接（D59），只能直接访问地址
export default [
  route("en?", "routes/lang.tsx", [
    index("routes/home.tsx"),
    route("plans", "routes/plans.tsx"),
    route("vendors", "routes/vendors.tsx"),
    route("codes", "routes/codes.tsx"),
    route("deal/:id", "routes/deal.tsx"),
    route("vendor/:slug", "routes/vendor.tsx"),
    route("about", "routes/about.tsx"),
    route("status", "routes/status.tsx"),
    route("*", "routes/not-found.tsx"),
  ]),
] satisfies RouteConfig;
