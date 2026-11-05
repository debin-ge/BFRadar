import { data } from "react-router";

// 对不上路由的地址：抛 404，由根的 ErrorBoundary 按语言渲染"没有找到这一页"
export function loader() {
  throw data(null, { status: 404 });
}

export default function NotFound() {
  return null;
}
