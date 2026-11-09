// 后端内部页面数据（app/web/internal.py）的形状。文字都已按语言格式化好。

export type Seo = {
  title: string;
  description: string;
  canonical: string;
  alternates: { zh: string; en: string };
  site_url: string;
  og: {
    type: string;
    title: string;
    description: string;
    url: string;
    site_name: string;
    locale: string;
    image: string | null;
  };
  jsonld: Record<string, unknown>[];
};

export type Chrome = {
  countdown: {
    target: string;
    end: string;
    phase: "before" | "during" | "after";
    days: number;
    hours: number;
    minutes: number;
    seconds: number;
    text: string;
    when: { before: string; during: string };
  };
  freshness: string;
  footer: string;
};

export type PlanRow = {
  name: string;
  /** NAT 的提示；其余为 null */
  ip: string | null;
  /** 线路与网络标签（O-16），已按语言写好，如 CN2 GIA */
  net: string[];
  cpu: string;
  ram: string;
  disk: string;
  bw: string;
  port: string;
  loc: string;
  price: string;
  /** 合约期与续费价（D85），如"24 个月预付、续费 $11.99/月"；原文没写为空 */
  term: string;
  cny: string;
  /** 套餐自带的优惠码（原样，页面加"码"字样与复制按钮） */
  code: string | null;
  order: string | null;
  /** 商家页的在售套餐汇总里，指向所属优惠的详情页 */
  href?: string;
};

/** 套餐表的列（后端按这批套餐里有没有值决定画哪几列，顺序固定） */
export type PlanCol = "name" | "cpu" | "ram" | "disk" | "bw" | "port" | "loc" | "price" | "cny" | "order";

/** 有效期标记（O-15）：ended 已结束、soon 剩 3 天以内、open 其余；原帖没写截止日期时整个为 null */
export type Expiry = { state: "ended" | "soon" | "open"; text: string; date: string };

export type DealCard = {
  id: number;
  href: string;
  bf: boolean;
  event: string;
  vendor: string;
  vendor_href: string | null;
  heat: string | null;
  heat_title: string | null;
  title: string;
  /** "同一活动另有 N 帖"，链到详情页的 #same */
  same: string | null;
  same_href: string | null;
  flags: { key: string; label: string }[];
  /** 折扣与续费性质；kind 决定颜色：discount、rec-recurring、rec-first_term、rec-mixed、rec-unknown */
  offer: { kind: string; text: string }[];
  specs: string[];
  /** 各套餐线路标签的并集 */
  net: string[];
  codes: string[];
  codes_more: string | null;
  price: [string, string] | null;
  date: string;
  published_at: string | null;
  expiry: Expiry | null;
  expand: string | null;
  plans: PlanRow[];
  cols: PlanCol[];
  plans_more: string | null;
  excerpt: string | null;
};

/** "24 小时动态"的一行：kind 为 new、price_drop、new_code，或不足时补上的 earlier */
export type Tag = {
  id: number;
  href: string;
  vendor: string;
  price: string | null;
  cny: string;
  kind: string;
  kind_label: string;
  ago: string;
  at: string | null;
};

export type Option = { value: string; label: string; selected: boolean };
export type Pager = {
  text: string;
  prev: string | null;
  next: string | null;
  /** 页码；null 是中间省略的地方 */
  pages: ({ n: number; href: string; current: boolean } | null)[];
} | null;
export type IntelItem = { title: string; url: string | null; ago: string };
export type VendorLink = { vendor: string; vendor_href: string | null; heat: string | null; heat_title: string | null };

export type PlansPage = {
  seo: Seo;
  scopes: { key: string; href: string; current: boolean }[];
  /** 快捷筛选（O-19）：每个按钮是一条现成的查询，当前查询与它一致时 current，再点一下回到没有筛选 */
  presets: { key: string; label: string; href: string; current: boolean }[];
  form: {
    action: string;
    scope: string | null;
    cpu: Option[];
    ram: Option[];
    disk: Option[];
    price_max: string;
    location: Option[];
    period: Option[];
    nvme: boolean;
    no_nat: boolean;
    /** 线路勾选项：当前范围里有套餐带的标签，加上已选的 */
    net: { value: string; label: string; checked: boolean }[];
    products: { value: string; checked: boolean }[];
    q: string;
    region: Option[];
    sorts: Option[];
    reset: string | null;
  };
  total: string;
  rows: (PlanRow & {
    /** 机房多于两个时的全部机房（悬停显示） */
    loc_title: string | null;
    vendor: string;
    vendor_href: string | null;
    deal_title: string;
    deal_href: string;
  })[];
  pager: Pager;
};

export type DealPage = VendorLink & {
  seo: Seo;
  bf: boolean;
  event: string;
  title: string;
  original_title: string | null;
  summary: string | null;
  url: string | null;
  purchase_url: string | null;
  facts: {
    expiry: Expiry | null;
    price: [string, string] | null;
    products: string;
    discount: string;
    discount_note: string | null;
    recurring: string;
    codes: string[];
    published: string;
    seen: string;
  };
  plans_heading: string;
  plans: PlanRow[];
  cols: PlanCol[];
  excerpt: string | null;
  changes: { at: string; time: string; text: string }[];
  same_heading: string | null;
  same: { title: string; href: string; meta: string }[];
  others_heading: string | null;
  others: { title: string; href: string; meta: string }[];
  intel: IntelItem[];
};

type VendorDeal = { title: string; href: string; event: string | null; expiry: Expiry | null; meta: string };

export type VendorPage = {
  seo: Seo;
  name: string;
  aka: string | null;
  website: string | null;
  /** 本季在售套餐的机房：地区 → 城市与套餐数（O-17） */
  places: { region: string; cities: string }[];
  /** 线路标签（O-16） */
  net: string[];
  lowest: { price: [string, string]; title: string; href: string; date: string } | null;
  plans_heading: string | null;
  plans: PlanRow[];
  cols: PlanCol[];
  deals_heading: string;
  deals: VendorDeal[];
  past_heading: string | null;
  past: VendorDeal[];
  intel: IntelItem[];
};

/** 优惠码汇总（O-18） */
export type CodesPage = {
  seo: Seo;
  total: string;
  rows: {
    id: number;
    href: string;
    vendor: string;
    vendor_href: string | null;
    title: string;
    codes: string[];
    discount: string;
    recurring: string;
    expiry: Expiry | null;
    date: string;
  }[];
  empty: string;
};

/** 商家目录（O-17） */
export type VendorsPage = {
  seo: Seo;
  total: string;
  sorts: { value: string; label: string; current: boolean; href: string }[];
  rows: {
    name: string;
    href: string;
    aliases: string;
    deals: string;
    has_deals: boolean;
    low: string;
    latest: string;
    regions: string;
  }[];
  empty: string;
};

export type AboutPage = {
  seo: Seo;
  scope_bf: string;
};

/** 采集状态页：概况的每一格与各源最近 10 轮的表，文字都由后端按语言格式化好。 */
export type StatusPage = {
  seo: Seo;
  refreshing: boolean;
  facts: { label: string; value: string; note: string | null }[];
  sources: {
    key: string;
    name: string;
    meta: string[];
    failing: string | null;
    columns: string[];
    rows: { bad: boolean; started_at: string; time: string; took: string; cells: string[]; result: string }[];
    empty: string;
  }[];
};

export type Home = {
  seo: Seo;
  stats: { bf: string; new: string; plans: string; vendors: string };
  just: Tag[];
  scopes: { key: string; href: string; current: boolean }[];
  form: {
    action: string;
    scope: string | null;
    products: { value: string; checked: boolean }[];
    q: string;
    sorts: { value: string; selected: boolean }[];
    /** 含已结束的活动；"含往季"不处理已结束的，为 null（页面不给这个勾选项） */
    ended: boolean | null;
    reset: string | null;
  };
  total: string;
  deals: DealCard[];
  pager: Pager;
  /** 黑五档末尾的提示：本季另有 N 条其他优惠，链到"本季全部" */
  more: { text: string; href: string } | null;
  empty: string;
  intel: { title: string; url: string | null; ago: string }[];
};
