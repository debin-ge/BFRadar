import type { ComponentProps } from "react";

// 少量界面图标直接内联（不引 lucide-react：它的最新版常常不满足 pnpm 的最短发布时间策略）
export function ChevronDownIcon(props: ComponentProps<"svg">) {
  return (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" {...props}>
      <path d="m6 9 6 6 6-6" />
    </svg>
  );
}
