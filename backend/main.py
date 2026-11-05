"""黑五雷达（BFRadar）命令行入口（《实施计划》M1.14、M2.1）。

serve（默认：后端接口与调度器）/ crawl / llm-backfill / reprocess / fx-update / scheduler / stats / show / report-gold /
export-gold / init-db / config。report-gold、export-gold 依赖 tests/ 下的金标集工具，只在开发环境可用。
"""

import argparse
import logging
import signal
import sys
import threading
import time
from datetime import timedelta

from app.config import ROOT, ConfigError, Settings, load_settings
from app.db import DataDirError, Database, prepare_data_dir
from app.i18n import translator

TOKENS_PER_POST = 7900  # 附录 A 实测：约 7.9k token/条


def setup_logging(level: str) -> None:
    """结构化日志到 stdout（第 9 节可观测性），时间为 UTC。"""
    logging.Formatter.converter = time.gmtime
    logging.basicConfig(
        stream=sys.stdout,
        level=level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%SZ",
    )
    if level != "DEBUG":  # httpx 每个请求一行、APScheduler 每次运行任务一行 INFO，太吵
        logging.getLogger("httpx").setLevel(logging.WARNING)
        logging.getLogger("apscheduler").setLevel(logging.WARNING)


def open_db(settings: Settings) -> Database:
    from app.vendors import prepare

    db = Database(settings.db_path)
    prepare(db)  # 迁移并同步商家别名表
    return db


# ---------------------------------------------------------------- 采集与抽取


def cmd_init_db(settings: Settings, args: argparse.Namespace) -> int:
    before, after = Database(settings.db_path).migrate()
    if before == after:
        print(f"数据库已是最新版本 {after}：{settings.db_path}")
    else:
        print(f"数据库迁移 {before} → {after}：{settings.db_path}")
    return 0


def cmd_crawl(settings: Settings, args: argparse.Namespace) -> int:
    from app.http import HttpClient
    from app.pipeline import crawl
    from app.sources import build_sources

    db = open_db(settings)
    with HttpClient(settings) as http:
        run_id, reports = crawl(db, build_sources(settings, http, args.source), reason="cli")
    print(f"\n运行 #{run_id}")
    print(f"{'源':<14}{'抓取':>5}{'新增':>5}{'更新':>5}  {'新分类 优惠/情报/无关':<14}  {'优惠 新/改/变更':<12} {'情报+':>4}  备注")
    for r in reports:
        note = r.error or ", ".join(f"{k}={v}" for k, v in r.stats.items() if k != "errors")
        if r.stats.get("errors"):
            note += f"；{len(r.stats['errors'])} 个入口失败"
        if r.post_errors:
            note += f"；{len(r.post_errors)} 条处理出错"
        kinds = "/".join(str(r.kinds.get(k, 0)) for k in ("offer", "intel", "ignore")) if r.kinds else "-"
        deals = "/".join(str(r.deals.get(k, 0)) for k in ("created", "updated", "changes"))
        print(f"{r.source:<14}{r.fetched:>5}{len(r.new):>5}{len(r.updated):>5}  {kinds:<14}  {deals:<12} "
              f"{r.intel:>4}  {note}")
    if not settings.llm_enabled:
        print("\nLLM 未配置：规则模式")
    elif args.llm_limit > 0:
        run_llm(db, settings, limit=args.llm_limit)
    return 1 if any(r.error for r in reports) else 0


def run_llm(db: Database, settings: Settings, *, limit: int | None, backfill: bool = False) -> int:
    from app.extract.llm import LlmClient
    from app.pipeline import run_llm_queue

    with LlmClient(settings) as client:
        r = run_llm_queue(db, client, settings, limit=limit, backfill=backfill)
    rate = f"{r.rejected / r.units:.1%}" if r.units else "-"
    print(f"\nLLM：待调用 {r.candidates}，调用 {r.calls}，完成 {r.done}，失败 {r.failed}，作废 {r.stale}；"
          f"校验单元 {r.units}，拒绝 {r.rejected}（{rate}）；token {r.tokens['total_tokens']}")
    for error in r.errors[:5]:
        print(f"  {error}")
    return 0 if r.calls == 0 or r.done else 1


def cmd_llm_backfill(settings: Settings, args: argparse.Namespace) -> int:
    from app.pipeline import llm_candidates

    if not settings.llm_enabled:
        print("LLM 未配置：在 .env 中填写 LLM_BASE_URL / LLM_API_KEY / LLM_MODEL", file=sys.stderr)
        return 2
    db = open_db(settings)
    n = len(llm_candidates(db.conn(), backfill=True, limit=args.limit))
    print(f"待补抽 {n} 条（最新发布的优先），预计约 {n * TOKENS_PER_POST / 10000:.0f} 万 token")
    if args.dry_run or n == 0:
        return 0
    return run_llm(db, settings, limit=args.limit, backfill=True)


def cmd_intel_translate(settings: Settings, args: argparse.Namespace) -> int:
    """社区讨论标题的机器翻译（O-14）：本季黑五情报里还没有另一种语言标题的，最新的优先。"""
    from app.extract.llm import LlmClient
    from app.translate import BATCH, candidates, run_translate
    from app.web import fmt

    db = open_db(settings)
    n = len(candidates(db.conn(), fmt.season_start_iso(settings.season_start), args.limit))
    print(f"待翻译标题 {n} 条，约 {-(-n // BATCH)} 次调用")
    if args.dry_run or n == 0:
        return 0
    if not settings.llm_enabled:
        print("LLM 未配置：在 .env 中填写 LLM_BASE_URL / LLM_API_KEY / LLM_MODEL", file=sys.stderr)
        return 2
    with LlmClient(settings) as client:
        r = run_translate(db, client, settings, limit=args.limit)
    print(f"调用 {r.calls} 次，译好 {r.done} 条，作废 {r.failed} 条；token {r.tokens}")
    for error in r.errors[:5]:
        print(f"  {error}")
    return 1 if r.errors and not r.done else 0


def cmd_reprocess(settings: Settings, args: argparse.Namespace) -> int:
    from app.pipeline import reprocess

    r = reprocess(open_db(settings), settings, args.source)
    print(f"重算 {r.fetched} 条原文：分类 {dict(r.kinds)}；优惠 新 {r.deals['created']} / 改 {r.deals['updated']} / "
          f"变更 {r.deals['changes']}；新增情报 {r.intel}；出错 {len(r.post_errors)}")
    for error in r.post_errors[:5]:
        print(f"  {error}")
    return 1 if r.post_errors else 0


def cmd_fx_update(settings: Settings, args: argparse.Namespace) -> int:
    from app.fx import update_rates
    from app.http import HttpClient
    from app.models import CURRENCIES, iso, utcnow

    db = open_db(settings)
    with HttpClient(settings) as http:
        result = update_rates(db.conn(), http, settings, now=iso(utcnow()))
    for name, error in result.errors.items():
        print(f"{name} 失败：{error}")
    if result.rates is None:
        print("没有可用的汇率（FX_PROVIDER 不含 static 且库里没有汇率）")
        return 1
    fx = result.rates
    print(f"生效汇率：{fx.provider} {fx.date}（{'本次拉取' if result.fetched else '沿用'}）")
    for cur in CURRENCIES:
        if cur != "CNY":
            print(f"  1 {cur} = {fx.rates[cur]:.4f} CNY")
    print(f"重算人民币估算：起价有变化的优惠 {result.refreshed} 条")
    return 0 if result.fetched else 1


def cmd_serve(settings: Settings, args: argparse.Namespace) -> int:
    """后端（接口、RSS、给前端的页面数据）与调度器同进程（D25）：Uvicorn 单 worker，调度器在应用生命周期里启停。"""
    import uvicorn

    from app.scheduler import Scheduler
    from app.web.app import create_app

    t = translator("zh")
    for key in settings.launch_warnings():
        logging.getLogger("bfradar").warning("上线配置：%s（上线前要设置）", t(key))
    scheduler = None if getattr(args, "no_scheduler", False) else Scheduler(settings)
    app = create_app(settings, scheduler=scheduler)
    uvicorn.run(app, host=getattr(args, "host", None) or settings.host, port=getattr(args, "port", None) or settings.port,
                workers=1, log_config=None, proxy_headers=True, forwarded_allow_ips="*")
    return 0


def cmd_scheduler(settings: Settings, args: argparse.Namespace) -> int:
    """前台运行调度器（不含接口），Ctrl-C 或 SIGTERM 停止；M2 起由 serve 启动。"""
    from app.scheduler import Scheduler

    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):  # 显式接管：后台启动时 SIGINT 默认被忽略
        signal.signal(sig, lambda *_: stop.set())
    scheduler = Scheduler(settings)
    scheduler.start()
    print(f"调度器运行中（{'黑五周' if scheduler.tier() == 'bf' else '常态'}档位），Ctrl-C 停止")
    while not stop.wait(60):
        pass
    print("正在停止（等待进行中的任务结束）…")
    scheduler.shutdown(wait=True)
    return 0


# ---------------------------------------------------------------- 查看


def cmd_stats(settings: Settings, args: argparse.Namespace) -> int:
    from app.models import iso, utcnow
    from app.repo import site_stats

    s = site_stats(open_db(settings).conn(), season_start=settings.season_start.isoformat(),
                   since=iso(utcnow() - timedelta(hours=24)))
    d = s["deals"]
    print(f"优惠 {d['total']}（本季 {d['season']}，本季黑五 {d['bf']}，24 小时新增 {d['new']}）；套餐 {s['plans']}；"
          f"情报 {s['intel']['total']}（黑五 {s['intel']['bf']}）；商家 {s['vendors']}")
    print("LLM：" + ("，".join(f"{k} {v}" for k, v in sorted(s["llm"].items())) or "-"))
    fx = s["fx"]
    print(f"汇率：{fx['provider']} {fx['date']}（拉取于 {fx['fetched_at']}）" if fx else "汇率：库里没有，使用内置表")
    print("原文：" + "，".join(f"{k} {v}" for k, v in s["raw"].items()))
    print(f"\n{'源':<14}{'最近一轮':<22}{'最近成功':<22}{'连续失败':>8}{'抓取':>6}{'新增':>6}")
    for source, r in sorted(s["runs"].items()):
        print(f"{source:<14}{r['last_at']:<22}{(r['last_ok_at'] or '-'):<22}{r['failing']:>8}{r['fetched']:>6}"
              f"{r['new']:>6}")
    return 0


def _price(deal) -> str:
    from app.fx import format_price

    if deal.min_price is None:
        return "起价未知"
    return format_price(deal.min_price, deal.min_price_currency, deal.min_price_period, "zh",
                        deal.min_price_cny_year)


def cmd_show(settings: Settings, args: argparse.Namespace) -> int:
    from app.repo import load_deal
    from app.textutil import normalize_url

    conn = open_db(settings).conn()
    if args.url:
        deal = load_deal(conn, normalize_url(args.url))
        if deal is None:
            print(f"库里没有这条优惠：{args.url}", file=sys.stderr)
            return 1
        show_deal(conn, deal)
        return 0
    urls = [r[0] for r in conn.execute("SELECT url FROM deals ORDER BY published_at DESC, id DESC LIMIT ?",
                                       (args.latest,))]
    for url in urls:
        d = load_deal(conn, url)
        codes = "、".join(d.coupon_codes) or "无码"
        print(f"{'[黑五] ' if d.is_black_friday else ''}{d.vendor or '商家未知'} · {_price(d)} · {codes} · "
              f"{len(d.plans)} 个套餐 · {(d.published_at or '')[:10]}")
        print(f"  {d.title}\n  {d.url}")
    return 0


def show_deal(conn, d) -> None:
    from app.fx import format_price
    from app.repo import deal_changes

    print(f"{d.title}\n{d.url}")
    if d.title_zh:
        print(f"中文标题：{d.title_zh}")
    print(f"商家 {d.vendor} · 来源 {'、'.join(d.sources)} · 发布 {d.published_at} · 首次看到 {d.first_seen_at} · "
          f"最后看到 {d.last_seen_at}")
    print(f"活动 {d.event_type}{'（黑五）' if d.is_black_friday else ''} · 产品 {'、'.join(map(str, d.product_types))} · "
          f"折扣 {d.discount_pct or '-'}%（{d.recurring}）{d.discount_text or ''} · 码 {'、'.join(d.coupon_codes) or '-'}")
    print(f"起价 {_price(d)} · 购买 {d.purchase_url or '-'}")
    print(f"形态 {d.plan_format} · 完整度 {d.completeness} · 抽取 {d.extracted_by} · LLM {d.llm_status}"
          f"（{d.llm_attempts} 次）")
    for label, text in (("摘要", d.summary_zh), ("Summary", d.summary_en)):
        if text:
            print(f"{label}：{text}")
    if d.plans:
        print(f"\n{'#':>2} {'名称':<22}{'CPU':>5}{'内存':>7}{'存储':>8}{'流量TB':>7}{'端口':>6}  {'价格':<26}{'码':<12}机房")
        for p in d.plans:
            price = format_price(p.price, p.currency, p.period, "zh", p.price_cny_year) if p.price else "-"
            print(f"{p.position:>2} {(p.name or '-')[:21]:<22}{p.cpu_cores or '-':>5}{p.ram_gb or '-':>7}"
                  f"{p.disk_gb or '-':>8}{p.bandwidth_tb or '-':>7}{p.port_gbps or '-':>6}  {price:<26}"
                  f"{(p.coupon_code or '-'):<12}{'、'.join(p.locations)}")
    elif d.price_list:
        print("价格列表：" + "；".join(format_price(x["amount"], x["currency"], x["period"], "zh")
                                    + ("（起）" if x.get("from") else "") for x in d.price_list))
    if d.excerpt:
        print(f"\n摘录：{d.excerpt}")
    changes = deal_changes(conn, d.id)
    if changes:
        print("\n变更：")
        for c in changes:
            print(f"  {c['changed_at']} {c['field']}：{c['old_value']} → {c['new_value']}")


# ---------------------------------------------------------------- 金标集（开发环境）


def dev_tools():
    """导入 tests/ 下的金标集工具；部署环境没有 tests/ 时给出明确错误。"""
    tests = ROOT / "tests"
    if not (tests / "gold_eval.py").exists():
        raise SystemExit("找不到 tests/gold_eval.py：report-gold 与 export-gold 只在开发环境可用")
    if str(tests) not in sys.path:
        sys.path.insert(0, str(tests))
    import gold_eval  # noqa: PLC0415  （导入时把 tests/tools 加入 sys.path）
    import snapshot  # noqa: PLC0415

    return gold_eval, snapshot


def cmd_report_gold(settings: Settings, args: argparse.Namespace) -> int:
    gold_eval, _ = dev_tools()
    mode = args.mode or ("llm" if settings.llm_enabled else "rules")
    return gold_eval.main(["--run", "--split", args.split, "--mode", mode])


def cmd_export_gold(settings: Settings, args: argparse.Namespace) -> int:
    """从库里按条件选出金标集 v1.1 候选，在快照池中找到原帖后生成空白草稿（金标集规范第 9 节）。"""
    gold_eval, snapshot = dev_tools()
    from app.textutil import normalize_url

    table = "deals" if args.kind == "offer" else "intel"
    sql = [f"SELECT r.url, r.source, r.title, r.published_at, x.is_black_friday AS bf"
           f"{', x.plan_format' if table == 'deals' else ''} FROM raw_posts r JOIN {table} x ON x.url = r.url"
           f" WHERE r.source IN ({', '.join('?' * len(gold_eval.SOURCES))})"]
    params: list = sorted(gold_eval.SOURCES)
    for cond, value in (("r.source = ?", args.source), ("x.plan_format = ?", args.format),
                        ("r.published_at >= ?", args.since)):
        if value:
            sql.append(f"AND {cond}")
            params.append(value)
    if args.bf:
        sql.append("AND x.is_black_friday = 1")
    rows = Database(settings.db_path).conn().execute(" ".join(sql) + " ORDER BY r.published_at DESC", params)
    pool = {normalize_url(item.url): item for item in snapshot.load_pool(since=None).values()}
    existing = {p.stem for p in gold_eval.GOLD.glob("*.json")}
    picked, missing = [], 0
    for r in rows:
        item = pool.get(r["url"])
        if item is None:
            missing += 1
            continue
        if item.id in existing:
            continue
        picked.append(item.id)
        print(f"{item.id:<14}{(r['published_at'] or '')[:10]:<12}{'BF' if r['bf'] else '  '} "
              f"{(r['plan_format'] if table == 'deals' else 'intel') or '-':<9}{r['title'][:80]}")
        if len(picked) >= args.limit:
            break
    if missing:
        print(f"另有 {missing} 条不在快照池中：先运行 uv run python tests/tools/snapshot.py 保存当前 feed")
    if picked and not args.dry_run:
        snapshot.write_drafts(picked)
    return 0


# ---------------------------------------------------------------- 其他


def cmd_config(settings: Settings, args: argparse.Namespace) -> int:
    for key, value in settings.redacted().items():
        print(f"{key:<18} {value}")
    print(f"{'llm_enabled':<18} {settings.llm_enabled}")
    t = translator("zh")
    for key in settings.launch_warnings():
        print(f"警告：{t(key)}（上线前要设置）")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="main.py", description="黑五雷达（BFRadar）")
    sub = parser.add_subparsers(dest="command", metavar="命令")
    p = sub.add_parser("serve", help="启动后端（接口与页面数据）与调度器（不带命令时的默认）")
    p.add_argument("--host", help="监听地址，默认 HOST")
    p.add_argument("--port", type=int, help="端口，默认 PORT")
    p.add_argument("--no-scheduler", action="store_true", help="只启动后端，不采集（开发前端时取数据用）")
    p.set_defaults(func=cmd_serve)
    sub.add_parser("init-db", help="创建或迁移数据库").set_defaults(func=cmd_init_db)

    p = sub.add_parser("crawl", help="立即采集一轮：分类、抽取、合并写库，再调用 LLM 处理最新的待处理优惠")
    p.add_argument("--source", action="append", metavar="ID", help="只采集指定源，可重复；默认按 SOURCES 全部")
    p.add_argument("--llm-limit", type=int, default=10, metavar="N",
                   help="采集后调用 LLM 处理的待处理优惠数，最新发布的优先（默认 10，0 为不调用；LLM 未配置时忽略）")
    p.set_defaults(func=cmd_crawl)

    p = sub.add_parser("llm-backfill", help="对全部未完成 LLM 抽取的优惠补抽（含满 3 轮失败的）")
    p.add_argument("--limit", type=int, metavar="N", help="最多处理 N 条，最新发布的优先")
    p.add_argument("--dry-run", action="store_true", help="只显示待补抽条数与预计 token")
    p.set_defaults(func=cmd_llm_backfill)

    p = sub.add_parser("reprocess", help="规则或分类改进后重算库里的原文（不抓取、不调用 LLM）")
    p.add_argument("--source", action="append", metavar="ID", help="只重算指定源，可重复")
    p.set_defaults(func=cmd_reprocess)

    p = sub.add_parser("intel-translate", help="翻译本季黑五社区讨论的标题（中文帖译英文、英文帖译中文）")
    p.add_argument("--limit", type=int, help="最多翻译多少条（最新的优先），默认全部")
    p.add_argument("--dry-run", action="store_true", help="只显示待翻译条数")
    p.set_defaults(func=cmd_intel_translate)

    sub.add_parser("fx-update", help="立即更新汇率并重算人民币估算").set_defaults(func=cmd_fx_update)
    sub.add_parser("scheduler", help="前台运行调度器（不含接口），Ctrl-C 停止").set_defaults(func=cmd_scheduler)
    sub.add_parser("stats", help="库里的总数、LLM 状态、汇率与各源最近运行").set_defaults(func=cmd_stats)

    p = sub.add_parser("show", help="查看优惠：最新几条，或按原帖 URL 看一条的全部字段与套餐表")
    p.add_argument("--latest", type=int, default=5, metavar="N", help="最新发布的 N 条（默认 5）")
    p.add_argument("--url", help="原帖 URL（任意写法，会先规范化）")
    p.set_defaults(func=cmd_show)

    p = sub.add_parser("report-gold", help="用当前抽取对金标集计分（开发环境）")
    p.add_argument("--split", choices=["dev", "holdout", "all"], default="all",
                   help="默认调试集与留出集分列（留出集只在 M1.15 验收时运行，D20）")
    p.add_argument("--mode", choices=["rules", "llm"], help="默认 LLM 已配置时用 llm，否则 rules")
    p.set_defaults(func=cmd_report_gold)

    p = sub.add_parser("export-gold", help="从库里选出金标集 v1.1 候选并生成空白草稿（开发环境）")
    p.add_argument("--kind", choices=["offer", "intel"], default="offer")
    p.add_argument("--source", help="只选该源，如 lowendtalk")
    p.add_argument("--bf", action="store_true", help="只选黑五帖")
    p.add_argument("--format", choices=["pipe", "table", "list", "freetext", "none"], help="只选该套餐形态")
    p.add_argument("--since", metavar="YYYY-MM-DD", help="只选该日之后发布的")
    p.add_argument("--limit", type=int, default=5, metavar="N", help="最多 N 条（默认 5）")
    p.add_argument("--dry-run", action="store_true", help="只列出候选，不生成草稿")
    p.set_defaults(func=cmd_export_gold)

    sub.add_parser("config", help="显示生效的配置（密钥只显示是否已设置）").set_defaults(func=cmd_config)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        settings = load_settings()
    except ConfigError as e:
        print(f"配置错误：{e}", file=sys.stderr)
        return 2
    setup_logging(settings.log_level)
    if args.command != "config":  # 配置已读完（含 .env），之后可以降权；config 只显示配置，不碰数据目录
        try:
            prepare_data_dir(settings.db_path)
        except DataDirError as e:
            print(f"数据目录错误：{e}", file=sys.stderr)
            return 2
    if args.command is None:  # 不带命令即 serve（第 10.3 节）
        return cmd_serve(settings, args)
    return args.func(settings, args)


if __name__ == "__main__":
    sys.exit(main())
