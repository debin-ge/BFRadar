"""LLM 抽取层（《需求与方案说明》第 12.5 节、附录 C）：OpenAI 兼容 chat/completions，厂商无关（D07）。

只对优惠记录调用。请求 temperature=0 与 response_format=json_object；服务端不支持这两项时（400 且报错提到该参数）
去掉该参数重试并在本客户端内记住，此后改由提示词约束 JSON，并从回复中截取首个完整 JSON 对象。
网络错误、429、5xx 与无法解析的回复重试一次，仍失败抛 LlmError，由流水线标记 llm_status=failed、下一轮再试
（状态机见文件末尾 llm_due / llm_after，D33、D44）。输出一律经 validate.py 反幻觉校验后才使用。
"""

import json
import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field

import httpx

from ..config import Settings
from ..models import CURRENCIES, LlmStatus
from ..textutil import Link
from . import keywords as kw

log = logging.getLogger(__name__)

PROMPT_VERSION = "3"  # 提示词或 Schema 改动影响抽取结果时递增；llm_hash 含此版本，改动后历史记录会被重新抽取。D69 去掉 ipv4、D70 加 ends_at、D71 加 net 都没有递增：旧结果里多出或缺少的字段不碍事（缺的由规则层补），不值得重抽全部历史
MAX_TEXT_CHARS = 12000
LEAD_CHARS = 1500
MAX_POST_ATTEMPTS = 3  # 因帖子本身失败（post_specific）达到此轮数后不再自动重试，留给 llm-backfill（D44）
POST_SPECIFIC_STATUS = {400, 413, 422}  # 与请求内容有关的拒绝（超长、内容过滤等）；鉴权、限流、5xx 与网络错误不怪帖子

SCHEMA = {
    "vendor": "string|null",
    "is_black_friday": "boolean",
    "event_type": "black_friday|cyber_monday|holiday|anniversary|launch|restock|giveaway|sale|other",
    "product_types": ["vps|dedicated|hosting|storage"],
    "discount": {"pct": "integer 1-100|null", "recurring": "recurring|first_term|mixed|unknown", "text": "string|null"},
    "coupons": [{"code": "string", "applies_to": "string|null"}],
    "ends_at": "YYYY-MM-DD|null",
    "plans": [{
        "name": "string|null", "cpu_cores": "number|null", "cpu_desc": "string|null", "ram_gb": "number|null",
        "disk_gb": "number|null", "disk_type": "NVMe|SSD|HDD|null", "bandwidth_tb": "number|null",
        "port_gbps": "number|null", "locations": ["string"], "net": ["cn2gia|cn2gt|9929|4837|cmin2|cmi|bgp|native_ip|3net"],
        "price": "number",
        "currency": "|".join(CURRENCIES), "period": "month|quarter|semi|year|biennial|triennial|onetime|hour|null",
        "price_note": "promo|regular|null", "order_link_index": "integer|null", "coupon_code": "string|null",
        "notes": "string|null",
    }],
    "purchase_link_index": "integer|null",
    "summary_zh": "string（≤60 字）",
    "summary_en": "string (≤140 chars)",
    "title_zh": "string|null",
    "title_en": "string|null",
    "confidence": "number 0-1",
}

SYSTEM_PROMPT = f"""你是 VPS / 服务器促销帖的结构化抽取器。只使用用户消息中给出的原文信息，不推断、不补全、不编造；原文没有的字段填 null。

规则：
1. 价格必须是原文出现的数字；优惠码必须与原文逐字一致（大小写敏感），原文写明已过期的码不要；链接只能用链接表的序号引用（order_link_index、purchase_link_index）。purchase_link_index 依次取：黑五专题页或活动落地页 → 第一个下单链接 → 商家主站；不选论坛、社交、评测与推广链接。
2. 每个套餐 = 原文中一个可独立购买的配置 × 一个付费周期：同一配置写了年付、两年付两个价格就输出两个套餐。不要把不同套餐的字段混在一起。只有配置没有价格、或只有 "from $5/mo" 这类起价的，不算套餐。赠品、加购项、续费价单独一行的，不算套餐（续费价写进该套餐 notes）。
3. 同时写原价与活动价（含用码后的价格）时取活动价，price_note=promo；只写原价、折扣需另用码时取原价，price_note=regular；括号里的其他货币换算价不要。
4. 单位：内存与存储为 GB（1 TB = 1000 GB，512 MB = 0.5），多块盘写总容量；流量为每月 TB，unmetered / unlimited 填 null；端口为 Gbps（100 Mbps = 0.1）。
5. 机房只填原文写明的城市或国家，标题里的 "UK VPS" 不算机房。
6. 是否黑五：原文明确把本次活动归为 Black Friday / Cyber Monday（含 pre-、early、countdown）才为 true；"今年不做黑五""比黑五还便宜" 为 false。event_type 多重命中时黑五优先。
7. 折扣：pct 为原文写出的最高百分比（"up to 60% off" 为 60，5 折为 50，cashback 不算）；recurring：续费同价、renewal prices unchanged / locked / price freeze、for life / lifetime、每次续费都打折为 recurring，仅首期 / 首月 / 续费恢复原价为 first_term，不同套餐分别写了两种为 mixed，都没写为 unknown；text 为原文中描述折扣的最短完整短语，逐字摘录，不要改写或拼接。
8. 商家 vendor：品牌名，按原文自称写法；自称有长短两种写法（VM6 / VM6 Networks）时取与官网或下单链接域名一致的那种；不加 LLC、Ltd、Inc 等后缀；不填发帖人的个人 ID（除非品牌就是它）。
9. 产品类型 product_types：只标本帖实际在售的，只有这四类。vps = KVM、VDS、NAT、LXC、云服务器，大硬盘的 Storage VPS 也是 vps；dedicated = 独服、bare metal；hosting = 虚拟主机、reseller；storage = 对象存储、S3、备份存储、网盘，不含存储型 VPS，VPS 附带的备份或快照也不算。邮箱、域名、托管机柜、CDN、授权等其他产品不标。
10. 摘要：summary_zh 用简体中文、summary_en 用英文，各一句，说明商家、活动、最低价与最值得注意的条件；summary_zh 不超过 60 字（汉字与全角标点算 1 字，英文字母、数字与半角符号算半字），summary_en 不超过 140 个字符，写不下就删次要条件，不要写半句；摘要里出现的价格必须是原文价格。title_zh 为英文原标题的中文翻译（中文帖填 null），title_en 为中文原标题的英文翻译（英文帖填 null）；商家名、套餐名、优惠码不翻译。
11. 截止日期 ends_at：原文写明的活动结束日期，格式 YYYY-MM-DD；没写年份按发布时间所在年份（12 月发帖写 1 月 5 日是下一年），"48 hours only"、"限时 3 天" 这类相对说法按发布时间推算；有几个不同的截止日期（活动、优惠码、不同产品）时取最晚的；没写明就填 null，不要猜。
12. 线路 net：只填原文明确写出该套餐（或整帖）使用的线路或网络，取值限 cn2gia（CN2 GIA）、cn2gt、9929（AS9929、CU VIP）、4837（AS4837、联通 169）、cmin2、cmi、bgp（BGP 多线）、native_ip（原生 IP）、3net（三网优化、三网回程）；"不支持 CN2" 这类否定不填；原文没写为空数组，不要凭商家名猜。
13. confidence 为你对整体抽取正确性的估计（0–1）。

只输出一个 JSON 对象，不要输出 JSON 以外的任何文字，结构如下：
{json.dumps(SCHEMA, ensure_ascii=False, indent=1)}"""


class LlmError(Exception):
    """post_specific：失败是否由这条帖子本身引起（计入 llm_attempts）；服务不可用、鉴权与限流不算。"""

    def __init__(self, message: str, *, post_specific: bool = False) -> None:
        super().__init__(message)
        self.post_specific = post_specific


@dataclass
class LlmResponse:
    data: dict
    usage: dict = field(default_factory=dict)
    raw: str = ""


def fit_text(text: str, limit: int = MAX_TEXT_CHARS) -> tuple[str, bool]:
    """超长正文优先保留开头与含价格的段落（及其前两行规格），按原顺序拼回；返回（文本, 是否删减）。"""
    if len(text) <= limit:
        return text, False
    lines = text.splitlines()
    keep: set[int] = set()
    used = 0
    for i, line in enumerate(lines):  # 开头
        if used + len(line) > LEAD_CHARS:
            break
        keep.add(i)
        used += len(line) + 1
    for i, line in enumerate(lines):  # 含价格的行及其前两行
        if kw.PRICE.search(line):
            for j in (i - 2, i - 1, i):
                if j >= 0 and j not in keep and used + len(lines[j]) + 1 <= limit:
                    keep.add(j)
                    used += len(lines[j]) + 1
    out, prev = [], -1
    for i in sorted(keep):
        if i != prev + 1:
            out.append("…")
        out.append(lines[i])
        prev = i
    return "\n".join(out), True


def build_messages(source: str, published_at: str | None, title: str, text: str, links: list[Link],
                   rules_hint: dict) -> list[dict]:
    body, trimmed = fit_text(text)
    link_table = "\n".join(f"[{lk.n}] {lk.anchor[:80]} → {lk.href}" for lk in links) or "（无）"
    user = (
        f"来源：{source}\n发布时间：{published_at or '未知'}\n标题：{title}\n\n"
        f"正文（结构化文本：表格单元以 | 分隔，列表项以 - 开头，链接以 [n] 标注）"
        f"{'；正文过长，已省略部分不含价格的段落，以 … 标出' if trimmed else ''}：\n<<<\n{body}\n>>>\n\n"
        f"链接表：\n{link_table}\n\n"
        f"规则层初步结果（可能有误，仅供参考，一切以原文为准）：\n{json.dumps(rules_hint, ensure_ascii=False)}"
    )
    return [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": user}]


def first_json_object(content: str) -> dict:
    """从回复中截取首个完整 JSON 对象（兼容代码块围栏与前后多余文字）。"""
    decoder = json.JSONDecoder()
    for m in re.finditer(r"\{", content):
        try:
            obj, _ = decoder.raw_decode(content, m.start())
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            return obj
    raise LlmError("回复中没有完整的 JSON 对象", post_specific=True)


class LlmClient:
    def __init__(self, settings: Settings, *, transport: httpx.BaseTransport | None = None,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        if not settings.llm_enabled:
            raise LlmError("LLM 未配置（LLM_BASE_URL / LLM_API_KEY / LLM_MODEL 三项齐全才启用）")
        self.model = settings.llm_model
        self._url = f"{settings.llm_base_url}/chat/completions"
        self._client = httpx.Client(
            headers={"Authorization": f"Bearer {settings.llm_api_key}", "Content-Type": "application/json"},
            timeout=settings.llm_timeout, proxy=settings.https_proxy or None, transport=transport,
        )
        self._sleep = sleep
        self.json_mode = True  # 服务端拒绝 response_format 后关闭
        self.send_temperature = True  # 服务端拒绝 temperature 后关闭
        self.calls = 0

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "LlmClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def complete(self, messages: list[dict]) -> LlmResponse:
        """一次抽取：可降级参数的 400 立即去掉参数重发（最多两次，不计重试）；其他失败重试一次。"""
        failures = 0
        while True:
            try:
                return self._once(messages)
            except _Downgraded:
                continue  # 每次降级关掉一个参数，至多发生两次
            except (LlmError, httpx.TransportError) as e:
                failures += 1
                if failures >= 2:
                    raise LlmError(str(e), post_specific=getattr(e, "post_specific", False)) from e
                log.warning("LLM 调用失败，5 秒后重试：%s", e)
                self._sleep(5)

    def _once(self, messages: list[dict]) -> LlmResponse:
        body: dict = {"model": self.model, "messages": messages}
        if self.send_temperature:
            body["temperature"] = 0
        if self.json_mode:
            body["response_format"] = {"type": "json_object"}
        self.calls += 1
        resp = self._client.post(self._url, json=body)
        if resp.status_code == 400:
            detail = resp.text[:500].lower()
            if self.json_mode and "response_format" in detail:
                log.warning("LLM 服务端不支持 response_format，改为提示词约束 JSON")
                self.json_mode = False
                raise _Downgraded
            if self.send_temperature and "temperature" in detail:
                log.warning("LLM 服务端不接受 temperature，此后不再发送")
                self.send_temperature = False
                raise _Downgraded
        if resp.status_code >= 400:
            raise LlmError(f"HTTP {resp.status_code}：{resp.text[:200]}",
                           post_specific=resp.status_code in POST_SPECIFIC_STATUS)
        try:
            payload = resp.json()
            content = payload["choices"][0]["message"]["content"] or ""
        except (ValueError, KeyError, IndexError, TypeError) as e:
            raise LlmError(f"回复结构不符合 chat/completions：{e}", post_specific=True) from e
        return LlmResponse(first_json_object(content), payload.get("usage") or {}, content)


class _Downgraded(Exception):
    """服务端拒绝了可选参数，已在客户端关闭，立即重发。"""


def rules_hint(ex) -> dict:
    """把规则层结果压缩成提示用的 JSON（只放 LLM 修正时有用的字段，套餐最多 30 个）。"""
    return {
        "vendor": ex.vendor, "coupons": ex.coupon_codes,
        "discount": {"pct": ex.discount_pct, "recurring": str(ex.recurring), "text": ex.discount_text},
        "plans": [{k: v for k, v in {
            "name": p.name, "cpu_cores": p.cpu_cores, "ram_gb": p.ram_gb, "disk_gb": p.disk_gb,
            "price": p.price, "currency": p.currency, "period": str(p.period) if p.period else None,
        }.items() if v is not None} for p in ex.plans[:30]],
    }


# ---------------------------------------------------------------- llm_status 状态机（D33、D44）


@dataclass(frozen=True)
class LlmState:
    """deals 表的 llm_status / llm_attempts / llm_hash 三列。"""

    status: LlmStatus = LlmStatus.PENDING
    attempts: int = 0
    hash: str | None = None


def llm_hash(content_hash: str) -> str:
    """抽取所依据的内容与提示词版本；任一变化都要重抽。"""
    return f"{content_hash}:p{PROMPT_VERSION}"


def llm_due(state: LlmState, content_hash: str, enabled: bool, *, backfill: bool = False) -> LlmState:
    """本轮调用前的状态，status 为 pending 表示本轮要调用。

    已按当前内容与提示词抽取过 → 保持 done；内容或提示词变了 → 计数清零重来；
    因帖子本身失败满 MAX_POST_ATTEMPTS 轮 → 保持 failed，只有 llm-backfill（backfill=True）再试；
    LLM 未配置 → skipped（配置后的下一轮或 llm-backfill 会处理）。
    """
    current = llm_hash(content_hash)
    if state.hash != current:
        return LlmState(LlmStatus.PENDING if enabled else LlmStatus.SKIPPED, 0, None)
    if state.status is LlmStatus.DONE:
        return state
    if state.status is LlmStatus.FAILED and state.attempts >= MAX_POST_ATTEMPTS and not backfill:
        return state
    return LlmState(LlmStatus.PENDING if enabled else LlmStatus.SKIPPED, state.attempts, state.hash)


def llm_after(state: LlmState, content_hash: str, error: LlmError | None = None) -> LlmState:
    """一次调用（含客户端内的一次重试）之后的状态：成功 done；失败 failed，只有帖子本身的失败计数。"""
    if error is None:
        return LlmState(LlmStatus.DONE, state.attempts + 1, llm_hash(content_hash))
    return LlmState(LlmStatus.FAILED, state.attempts + (1 if error.post_specific else 0), llm_hash(content_hash))
