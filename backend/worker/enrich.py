"""enrich_activity 完整版，见 04 §2.4。

边界约束：只判断"这篇文章本身是什么"，不做跨文章判断——那是 aggregate_activity 的事。

原文抓取三态 + Fallback A/B：
- 状态①：direct（httpx + trafilatura），唯一会下载配图的通道
- 状态②：Jina Reader 兜底（M1 实测 openai.com 挂 Cloudflare 反爬时补上），不下载图片
- 状态③ Fallback A：direct+Jina 都失败后，用 Playwright 无头浏览器渲染兜底
  （能过部分 JS 挑战/反爬，纯 HTTP 客户端做不到这点；旧系统用 Claude Code 的 WebFetch
  工具，本项目没有这个工具，改用真实浏览器渲染），同样不下载图片
- Fallback B：以上全部失败，仍要完整写入占位记录（保证下游双链引用不断链）

配图分级渲染只在状态①做——状态②③明确"跳过转换只翻译，不下载图片，图片保留原始外链"，
下载的图片存本地 Docker 卷（IMAGE_STORAGE_DIR），M6 前端需要挂载同一路径做静态资源服务。
"""

from __future__ import annotations

import hashlib
import html as html_lib
import ipaddress
import os
import re
import socket
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from pathlib import Path
from typing import NamedTuple
from urllib.parse import urljoin, urlparse

import httpx
import trafilatura
from instructor.core.exceptions import IncompleteOutputException, InstructorRetryException
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import sync_playwright
from temporalio import activity

from worker.db import upsert_enriched_article
from worker.llm_client import DEFAULT_MAX_TOKENS, call_structured
from worker.schemas import (
    ArticleGist,
    ArticleMetadata,
    ChunkTranslation,
    TitleTranslation,
    TranslationCompletenessReview,
)

USER_AGENT = "ainews-service/0.1 (+https://github.com/SkyJourney/ainews-project)"
TRANSLATE_MODEL = "deepseek-v4-flash"
GIST_MODEL = "deepseek-v4-flash"
METADATA_MODEL = "deepseek-v4-flash"

# 2026-07-08：分块翻译调用异常（IncompleteOutputException 截断 / InstructorRetryException
# JSON 格式错误）此前直接放弃保留原文，一次重试都没有——只有"没报异常但 CJK 占比过低"
# 才有重试（还是同一个模型）。真实批次里这两类异常反复出现，换 qwen3.6-flash 兜底重试
# 一次：InstructorRetryException 更像是 deepseek-v4-flash 结构化输出偶发的格式问题，换
# 模型直接命中；IncompleteOutputException 是分块内容密度高、默认 8000 token 预算不够，
# 单纯换模型不一定够，顺带把这次重试的 max_tokens 调大。见 _translate_chunk_with_retry。
_TRANSLATE_FALLBACK_MODEL = "qwen3.6-flash"
_TRANSLATE_FALLBACK_MAX_TOKENS = DEFAULT_MAX_TOKENS * 2
MAX_CHUNK_CHARS = 2000

# Jina Reader 兜底触发条件，照搬旧项目 fetch-with-assets.py 已验证的清单
# （JINA_TRIGGER_ERRORS = http_400/403/429/503/timeout）；其余状态码（如 404）视为
# 真实错误，不掩盖，直接抛出交给 Temporal 重试/失败。
_JINA_READER_BASE = "https://r.jina.ai/"
_JINA_TRIGGER_STATUS = {400, 403, 429, 503}
_JINA_TIMEOUT_SECONDS = 45.0  # Jina 内部跑 headless render，比直连更宽松

_PLAYWRIGHT_TIMEOUT_MS = 30000

# 04 §2.4 语言判断：标题含中文字符 / 正文前 N 字符 CJK 占比 >30%，视为已是中文
_CJK_RANGES = ((0x4E00, 0x9FFF), (0x3400, 0x4DBF), (0x3040, 0x30FF), (0xAC00, 0xD7A3))
_LANGUAGE_SAMPLE_CHARS = 500
_CJK_RATIO_THRESHOLD = 0.3

# 翻译完整性机械校验（04 §2.4 硬约束）
_TRANSLATION_CJK_RATIO_THRESHOLD = 0.5
_UNTRANSLATED_RESIDUE_PATTERNS = [
    re.compile(p) for p in (r"ltx_", r"<td[\s>]", r"<tr[\s>]", r'class="ltx')
]

# 配图下载（04 §2.4 配图抓取分级渲染，只在状态①启用）
IMAGE_STORAGE_DIR = Path(os.environ.get("IMAGE_STORAGE_DIR", "/app/media"))
# 用自定义 scheme（不是 /media 根相对路径）：trafilatura.extract() 传了 url= 参数会用
# urljoin 把 img src 解析成绝对地址——根相对路径会被错误解析成"文章原站域名/media/..."
# （实测踩过的坑，deepmind.google 域名下的文章会把我们本地图片路径解析成
# https://deepmind.google/media/...，这个域名根本不存在这个路径）。自定义 scheme
# 天然不受相对路径解析规则影响。M6 前端渲染时把 ainews-media:// 转换成真实服务路径。
IMAGE_URL_PREFIX = "ainews-media://"
MAX_IMAGE_BYTES = 15 * 1024 * 1024
IMAGE_TIMEOUT_SECONDS = 15.0
_IMG_TAG_RE = re.compile(r"<img\b([^>]*?)/?>", re.IGNORECASE)
_IMG_ATTR_RE = re.compile(r'(\w[\w\-]*)\s*=\s*(["\'])(.*?)\2', re.IGNORECASE | re.DOTALL)
_VIDEO_SRC_EXTS = (".mp4", ".webm", ".mov", ".avi", ".mkv", ".m4v")
_ICON_SRC_PATTERNS = [
    re.compile(r"/static/browse/.*?/icons/", re.IGNORECASE),
    # arxiv.org/html/<id> 全文页用的是另一套静态资源路径（/static/base/.../images/...），
    # 跟摘要页 /static/browse/.../icons/ 不是同一个——此前只覆盖了摘要页，全文页的
    # arXiv 官方 logo/吉祥物图标（smileybones）没被拦截，被当正文配图下载+引用
    # （真实批次实测发现每篇文章多出 1-2 张跟内容无关的 svg 图标）。
    re.compile(r"arxiv\.org/static/base/.*?/images/", re.IGNORECASE),
]
# 已知跟踪/分析像素域名（04 §2.4 配图分级渲染范围外的噪声）：这类 <img> 不是正文配图，
# M4 实测发现把它们当正文图处理（下载成功后改写成正常 <img> 标签）会让 trafilatura
# 把页脚噪声一起当"正文"保留，进而拖累翻译完整性校验。按域名而非单站点特判过滤，
# 属于有限、可维护的通用修复。
_TRACKING_PIXEL_DOMAINS = (
    "bat.bing.com", "google-analytics.com", "googletagmanager.com", "doubleclick.net",
    "facebook.com", "connect.facebook.net", "analytics.twitter.com", "px.ads.linkedin.com",
    "scorecardresearch.com", "hotjar.com",
)
_MIME_EXT_MAP = {
    "image/png": ".png", "image/jpeg": ".jpg", "image/gif": ".gif",
    "image/webp": ".webp", "image/svg+xml": ".svg", "image/avif": ".avif", "image/bmp": ".bmp",
}


def _is_cjk_char(ch: str) -> bool:
    code = ord(ch)
    return any(lo <= code <= hi for lo, hi in _CJK_RANGES)


def _cjk_ratio(text: str) -> float:
    if not text:
        return 0.0
    return sum(1 for ch in text if _is_cjk_char(ch)) / len(text)


def needs_translation(title: str, body_md: str) -> bool:
    """[Temporal 回放安全] workflows.py 的 EnrichArticleWorkflow 直接调用这个函数（不经过
    activity），用来决定是否要跑 translate_activity（04 §2.4 语言判断）——workflow 代码在
    replay 时会重新执行一遍，这类被直接调用的函数必须是确定性纯函数，不能引入网络请求/
    当前时间/随机数等副作用，否则会静默破坏回放确定性（见 .claude/memory/known_issues.md，
    同类标记见 content_hash/compute_word_count）。"""
    already_chinese = _cjk_ratio(title) > 0 or _cjk_ratio(body_md[:_LANGUAGE_SAMPLE_CHARS]) > _CJK_RATIO_THRESHOLD
    return not already_chinese


def _chunk_paragraphs(body_md: str, max_chars: int = MAX_CHUNK_CHARS) -> list[str]:
    """按段落贪心分块，避免长文本单次撑爆上下文（用户在 M1 明确要求的分段翻译）。

    单个段落本身超过 max_chars 时硬切成若干子块——此前只在段落之间做贪心分组，
    假设了"没有单个段落会超过 max_chars"，这在摘要级短文本上一直成立，但全文版
    arxiv 论文常见没有空行分隔的超长参考文献列表/附录代码块，单段轻松超过 2000 字符。
    这类超大分块喂给翻译模型会导致输出被 max_tokens（8000）截断，Instructor 直接
    抛 IncompleteOutputException 中断整个 activity（真实批次实测触发）。硬切虽然可能
    切在句子中间，但只用于这种"没有更细粒度可分割"的极端输入，比让整个翻译流程崩溃
    要好得多。
    """
    paragraphs = body_md.split("\n\n")
    chunks: list[str] = []
    current: list[str] = []
    current_len = 0

    for para in paragraphs:
        if len(para) > max_chars:
            if current:
                chunks.append("\n\n".join(current))
                current = []
                current_len = 0
            chunks.extend(para[i : i + max_chars] for i in range(0, len(para), max_chars))
            continue

        para_len = len(para) + 2
        if current and current_len + para_len > max_chars:
            chunks.append("\n\n".join(current))
            current = []
            current_len = 0
        current.append(para)
        current_len += para_len

    if current:
        chunks.append("\n\n".join(current))
    return chunks


# translate_activity 的 start_to_close_timeout 按分块数量动态估算（2026-07-09 新增）：
# 固定 1800s 在 131 分块的 arxiv 超大论文上真实撞线过（enrich_failed）——分块翻译进度
# 不做断点续传，心跳超时/网关抖动触发的重试都要从头重来，分块越多单次 attempt 需要的
# 时间预算越大。PER_CHUNK_TIMEOUT_SECONDS 没有按"顺利路径"（实测约 3s/chunk）估算，而是
# 按三层兜底重试链的中等重试开销取值：call_structured 单次最多 2 次 HTTP 尝试
# （Instructor max_retries=1）×60s read timeout=120s 是单次调用的理论上限，一个 chunk
# 若策略链两层都失败还会走安全切分递归，最坏情况下单个 chunk 可能触发 6-8 次独立调用；
# 30s/chunk 是分摊了"一定比例 chunk 触发重试"之后的保守均值，不是理论最坏值（那样会把
# 上限拉到不现实的量级）。
BASE_TRANSLATE_TIMEOUT_SECONDS = 300
PER_CHUNK_TIMEOUT_SECONDS = 30
MIN_TRANSLATE_TIMEOUT_SECONDS = 1800
MAX_TRANSLATE_TIMEOUT_SECONDS = 5400


def estimate_translate_timeout_seconds(body_md: str) -> int:
    """[Temporal 回放安全] 纯函数，workflows.py 直接调用（不经过 activity）估算
    translate_activity 该给多长的 start_to_close_timeout：按 _chunk_paragraphs
    实际会切出的分块数量动态放大，而不是用一个固定值硬顶所有文章。"""
    chunk_count = len(_chunk_paragraphs(body_md))
    return min(
        MAX_TRANSLATE_TIMEOUT_SECONDS,
        max(MIN_TRANSLATE_TIMEOUT_SECONDS, BASE_TRANSLATE_TIMEOUT_SECONDS + PER_CHUNK_TIMEOUT_SECONDS * chunk_count),
    )


# ---------------------------------------------------------------------------
# SSRF 防护：本模块抓的 URL 来自外部信息源内容（RSS/webfetch 抽取结果等），不完全
# 可信——恶意/被劫持的源可以在条目里塞一个指向 Docker 网络内部服务（db/redis）或云
# 元数据端点（169.254.169.254）的链接。抓取前校验目标地址，且不信任 httpx 内置的
# follow_redirects（它不会在每一跳都重新校验，可能被诱导跳到内网地址）。
# ---------------------------------------------------------------------------

class SSRFBlockedError(Exception):
    pass


_ALLOWED_URL_SCHEMES = {"http", "https"}
_MAX_REDIRECTS = 5


def _assert_public_url(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme not in _ALLOWED_URL_SCHEMES:
        raise SSRFBlockedError(f"不允许的 URL scheme：{parsed.scheme!r}（{url}）")
    hostname = parsed.hostname
    if not hostname:
        raise SSRFBlockedError(f"URL 缺少 host：{url}")

    try:
        addr_infos = socket.getaddrinfo(hostname, None)
    except socket.gaierror as exc:
        raise SSRFBlockedError(f"域名解析失败：{hostname}") from exc

    for info in addr_infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
            raise SSRFBlockedError(f"目标地址落在私有/内部网段，拒绝访问：{hostname} -> {ip}")


def _safe_get(url: str, **kwargs) -> httpx.Response:
    """带 SSRF 防护的 GET：每一跳重定向都重新校验目标地址再继续，不直接信任
    httpx 的 follow_redirects=True。
    """
    current_url = url
    for _ in range(_MAX_REDIRECTS + 1):
        _assert_public_url(current_url)
        response = httpx.get(current_url, follow_redirects=False, **kwargs)
        if response.is_redirect:
            location = response.headers.get("location")
            if not location:
                return response
            current_url = urljoin(current_url, location)
            continue
        return response
    raise SSRFBlockedError(f"重定向次数超过上限（{_MAX_REDIRECTS}）：{url}")


# ---------------------------------------------------------------------------
# 配图分级渲染（04 §2.4，只在状态①调用；状态②③明确不下载图片）
# ---------------------------------------------------------------------------

def _parse_img_attrs(attrs_str: str) -> dict:
    return {m.group(1).lower(): html_lib.unescape(m.group(3)) for m in _IMG_ATTR_RE.finditer(attrs_str)}


def _pick_img_src(attrs: dict) -> str | None:
    for key in ("data-src", "data-original", "src"):
        v = attrs.get(key)
        if v and not v.startswith("data:"):
            return v
    srcset = attrs.get("srcset")
    if srcset:
        m = re.match(r"(https?://\S+?)(?:\s+\d+[wx])?(?:$|,)", srcset)
        if m and not m.group(1).startswith("data:"):
            return m.group(1)
    return None


def _is_video_src(url: str) -> bool:
    path = url.lower().split("?", 1)[0].split("#", 1)[0]
    return path.endswith(_VIDEO_SRC_EXTS)


def _is_icon_src(url: str) -> bool:
    return any(p.search(url) for p in _ICON_SRC_PATTERNS)


def _is_tracking_pixel(url: str) -> bool:
    domain = urlparse(url).netloc.lower()
    return any(domain == d or domain.endswith(f".{d}") for d in _TRACKING_PIXEL_DOMAINS)


def _is_placeholder_pixel(attrs: dict) -> bool:
    """无内容占位图（如 1x1 跟踪像素）→ 完全跳过（04 §2.4）。"""
    return attrs.get("width") in ("0", "1") and attrs.get("height") in ("0", "1")


def _guess_ext(url: str, content_type: str | None) -> str:
    if content_type:
        base = content_type.split(";")[0].strip().lower()
        if base in _MIME_EXT_MAP:
            return _MIME_EXT_MAP[base]
    ext = Path(urlparse(url).path).suffix.lower()
    if ext == ".jpeg":
        return ".jpg"
    if ext in {".png", ".jpg", ".gif", ".webp", ".svg", ".avif", ".bmp"}:
        return ext
    return ".bin"


def _download_image(url: str, target_stem: Path, *, referer: str) -> dict:
    """返回 status（saved/failed）+ 对应字段，是配图分级渲染的判定依据（04 §2.4）。"""
    try:
        response = _safe_get(
            url,
            headers={"User-Agent": USER_AGENT, "Referer": referer, "Accept": "image/*,*/*;q=0.8"},
            timeout=IMAGE_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
    except (httpx.HTTPError, SSRFBlockedError) as exc:
        return {"status": "failed", "reason": str(exc)[:100]}

    if len(response.content) > MAX_IMAGE_BYTES:
        return {"status": "failed", "reason": "too_large"}

    ext = _guess_ext(url, response.headers.get("Content-Type"))
    if ext == ".bin":
        return {"status": "failed", "reason": "unknown_content_type"}

    target = target_stem.with_suffix(ext)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(response.content)
    return {"status": "saved", "url_path": f"{IMAGE_URL_PREFIX}{target.relative_to(IMAGE_STORAGE_DIR)}"}


def _process_images(html: str, *, base_url: str, article_key: str) -> str:
    """扫图→下载→改写 <img> 标签为分级渲染结果，返回改写后的 HTML 交给 trafilatura。"""
    today_dir = date.today().isoformat()
    matches = list(_IMG_TAG_RE.finditer(html))
    replacements = []
    seq = 0

    for m in matches:
        attrs = _parse_img_attrs(m.group(1))
        src_raw = _pick_img_src(attrs)
        alt = attrs.get("alt", "")

        if not src_raw or src_raw.startswith("data:"):
            replacements.append((m.start(), m.end(), ""))  # 无实际内容的占位符 img，完全跳过
            continue

        abs_url = urljoin(base_url, src_raw)

        if _is_placeholder_pixel(attrs) or _is_icon_src(abs_url) or _is_tracking_pixel(abs_url):
            replacements.append((m.start(), m.end(), ""))
            continue

        if _is_video_src(abs_url):
            new_tag = (
                f'<p>🎬 {html_lib.escape(alt or "演示视频")}（演示视频未归档）'
                f'<a href="{html_lib.escape(abs_url)}">查看视频</a></p>'
            )
            replacements.append((m.start(), m.end(), new_tag))
            continue

        seq += 1
        target_stem = IMAGE_STORAGE_DIR / today_dir / f"{article_key}-{seq:03d}"
        result = _download_image(abs_url, target_stem, referer=base_url)

        if result["status"] == "saved":
            new_tag = f'<img src="{html_lib.escape(result["url_path"])}" alt="{html_lib.escape(alt)}"/>'
        else:
            # 失败/跳过 → 占位块 + "查看原图"外链，不裸露原始长 URL（04 §2.4）
            new_tag = f'<p>📷 {html_lib.escape(alt or "配图")} <a href="{html_lib.escape(abs_url)}">查看原图</a></p>'
        replacements.append((m.start(), m.end(), new_tag))

    for start, end, new in reversed(replacements):
        html = html[:start] + new + html[end:]
    return html


# ---------------------------------------------------------------------------
# 已知站点噪声清理：某些站点的固定侧边栏/引用小部件文本 trafilatura 识别不出来是
# 噪声（不在 nav/aside 等语义标签里），M4 实测发现 arxiv.org 摘要页的"引用工具/
# 相关代码数据/推荐工具/arXivLabs"这几个固定区块会顺着正文一起抽出来，拖累翻译
# 完整性校验的 CJK 占比（大量未翻译的英文小节标题+说明文字）。按已知标记截断。
#
# 跟踪像素 markdown 引用清理：这一步对三个通道（direct/jina/playwright）的最终
# markdown 统一生效——_process_images 的跟踪像素过滤只覆盖 direct 通道自己下载的
# HTML，Jina/Playwright 通道直接拿到的是别人已经抽取好的 markdown，同样可能夹带
# 页脚跟踪像素（M4 实测：openai.com 走 Jina 通道时出现 bat.bing.com 跟踪链接）。
# ---------------------------------------------------------------------------

_TRACKING_PIXEL_MARKDOWN_RE = re.compile(
    r"!\[[^\]]*\]\(https?://(?:[\w.-]+\.)?(?:" + "|".join(re.escape(d) for d in _TRACKING_PIXEL_DOMAINS) + r")/[^)]*\)"
)


def _strip_tracking_pixel_refs(markdown: str) -> str:
    return _TRACKING_PIXEL_MARKDOWN_RE.sub("", markdown)

# "Keep reading" 是 openai.com 文章页"相关文章推荐"区块的固定标题，其后紧跟站点
# 页脚整片导航菜单（Research/Products/API Platform/.../Terms & Policies），跟
# arxiv 的 References & Citations 是同一类"已知区块，按标记截断"噪声。
_KNOWN_BOILERPLATE_RE = re.compile(
    r"[ \t]*#{1,6}[ \t]*(References & Citations|Bibliographic and Citation Tools|Keep reading)\b"
)


def _strip_known_boilerplate(markdown: str) -> str:
    """trafilatura 抽出的标题带 Markdown # 前缀（如 "### References & Citations"），
    截断位置要匹配到标题行本身，不能只做字面量子串查找（实测踩过这个坑）。
    """
    match = _KNOWN_BOILERPLATE_RE.search(markdown)
    return markdown[: match.start()].rstrip() if match else markdown


# openai.com 文章页顶部会重复渲染两遍站内导航（桌面/移动两套 DOM），Jina Reader
# 兜底通道拿到的是整页 markdown，没有 trafilatura 的正文定位能力，这段噪声会跟真正
# 的标题、正文一起被送去翻译。按已知的、跨文章稳定不变的头部标记截断（不含具体文章
# 信息，纯站点模板，可安全应用到所有源——不匹配时是无副作用的 no-op）。
_OPENAI_HEADER_NAV_RE = re.compile(
    r"^\[Skip to main content\]\([^)]*\).*?"
    r"\[Try ChatGPT\(opens in a new window\)\]\(https://chatgpt\.com/\)Login\n*",
    re.DOTALL,
)


def _strip_openai_header_nav(markdown: str) -> str:
    return _OPENAI_HEADER_NAV_RE.sub("", markdown, count=1)


# 整段重复内容去重：响应式页面常见同一 DOM 元素被桌面/移动两套布局重复渲染，
# M4 深度诊断实测发现 a16z"更多文章"侧栏、arxiv"查看许可证"图标块都逐字重复了
# 2-3 次，拖累翻译完整性校验的 CJK 占比。只保留第一次出现；只对足够长的段落生效，
# 避免误伤本来就会重复出现的短标记（如 "---" 分隔符）。
_DEDUP_MIN_PARAGRAPH_CHARS = 40


def _dedup_repeated_paragraphs(markdown: str) -> str:
    paragraphs = markdown.split("\n\n")
    seen: set[str] = set()
    kept: list[str] = []
    for para in paragraphs:
        normalized = para.strip()
        if len(normalized) >= _DEDUP_MIN_PARAGRAPH_CHARS:
            if normalized in seen:
                continue
            seen.add(normalized)
        kept.append(para)
    return "\n\n".join(kept)


# trafilatura/Jina/Playwright 抽出来的 markdown 正文开头常常自带一份文章标题（页面
# 自身的 H1），跟 documents.title 字段（独立存储，前端详情页单独渲染一次）重复，会
# 造成"标题渲染两遍"——此前只在 arxiv 全文页专门处理过，2026-07-07 抽查发现非
# arxiv 的普通新闻源同样会中招（真实批次：89 篇当日 original 里 11 篇能观察到明显
# 重复），这里挪到三通道共用的清洗管线里统一生效。只去掉第一行 H1，不做更激进的
# 处理（个别标题换行导致的孤立残留，比整篇重复渲染的视觉问题小得多）。
_LEADING_H1_RE = re.compile(r"^#[ \t]+.+?\n+")


def _strip_leading_title_h1(markdown: str) -> str:
    return _LEADING_H1_RE.sub("", markdown, count=1)


def _clean_fetched_markdown(markdown: str) -> str:
    """三个抓取通道共用的最终 markdown 清洗管线，按顺序：开头重复标题剥离 → 已知区块
    截断 → 站点头部导航剥离 → 跟踪像素引用清理 → 整段重复内容去重。"""
    markdown = _strip_leading_title_h1(markdown)
    markdown = _strip_known_boilerplate(markdown)
    markdown = _strip_openai_header_nav(markdown)
    markdown = _strip_tracking_pixel_refs(markdown)
    return _dedup_repeated_paragraphs(markdown)


# arxiv.org/abs/<id> 是摘要页，页面本身只有几百字的摘要 + "查看PDF/全文链接/许可"
# 侧边栏文字，从来不含论文全文——此前 _fetch_direct 直接抓这个 URL，trafilatura 能
# 抽出内容就判定"通道成功"，导致 100% 的 arxiv 来源文章都只存到了摘要（真实生产数据
# 验证过：83 篇全部如此，不是偶发）。arXiv 另有全文 HTML 渲染服务（/html/<id>），
# 但不保证一定存在——论文提交后渲染有延迟（实测提交 3 天后 88% 可用），复杂 LaTeX/
# 图表也可能永久渲染失败——因此只作为优先尝试，不存在时静默回退到下面本来就有的
# 摘要页抓取，不影响非 arxiv 来源、也不改变现有的 direct→jina→playwright 兜底链路。
_ARXIV_ABS_RE = re.compile(r"^https?://arxiv\.org/abs/([\w.]+)/?$", re.IGNORECASE)


def _arxiv_fulltext_url(url: str) -> str | None:
    m = _ARXIV_ABS_RE.match(url.strip())
    return f"https://arxiv.org/html/{m.group(1)}" if m else None


# 摘要页固定的头部结构：分类标题行 + 提交日期 + "Title:实际标题"行，三者都是重复/
# 元数据，不是正文；"Title:"这一行经常带 4 个以上前导空格，如果只清标题不清这段
# 缩进，后面"View PDFAbstract:"前缀和侧边栏都会保留原样缩进，被 markdown 解释成
# 代码块导致图片/标题语法原样显示成文字（真实截图复现过的问题）。
_ARXIV_ABS_HEADER_RE = re.compile(
    r"^#[^\n]*\n+\s*\[Submitted[^\]]*\]\n+\s*#+\s*Title:[^\n]*\n+", re.IGNORECASE
)
_ARXIV_ABS_VIEWPDF_PREFIX_RE = re.compile(r"^\s*View PDF\s*Abstract:\s*", re.IGNORECASE)
# 摘要页尾部固定的"全文链接/访问论文"侧边栏（许可图标 + "view license"说明文字），
# 不是正文内容，且同样有缩进导致的渲染问题，按已知标记整体截断（跟
# _strip_known_boilerplate 是同一种"按已知标记裁剪"手法，只是这段没有 Markdown
# 标题前缀，不能复用同一个正则）。
_ARXIV_ABS_SIDEBAR_RE = re.compile(r"\n+\s*Full-text links:.*", re.DOTALL | re.IGNORECASE)


def _clean_arxiv_abs_markdown(markdown: str) -> str:
    markdown = _ARXIV_ABS_HEADER_RE.sub("", markdown, count=1)
    markdown = _ARXIV_ABS_VIEWPDF_PREFIX_RE.sub("", markdown, count=1)
    markdown = _ARXIV_ABS_SIDEBAR_RE.sub("", markdown, count=1)
    return markdown.strip()


def _try_arxiv_fulltext(url: str) -> str | None:
    """尝试 arxiv 全文 HTML 端点；任何失败（404/超时/SSRF 拦截/trafilatura 抽取为空）
    都只返回 None，不抛异常——这只是 direct 通道内部的一次可选增强尝试，失败时调用方
    应该原样回退到摘要页，不应该因此跳过摘要页直接进入 Jina/Playwright 兜底链路。
    """
    fulltext_url = _arxiv_fulltext_url(url)
    if not fulltext_url:
        return None
    try:
        response = _safe_get(fulltext_url, headers={"User-Agent": USER_AGENT}, timeout=30.0)
    except (httpx.HTTPError, SSRFBlockedError):
        return None
    if response.status_code != 200:
        return None

    article_key = hashlib.sha256(fulltext_url.encode("utf-8")).hexdigest()[:12]
    html_with_images_processed = _process_images(response.text, base_url=fulltext_url, article_key=article_key)
    markdown = trafilatura.extract(
        html_with_images_processed,
        url=fulltext_url,
        output_format="markdown",
        with_metadata=False,
        include_images=True,
    )
    # 开头重复标题的剥离已经收进 _clean_fetched_markdown 统一处理，这里不用再单独调用。
    return _clean_fetched_markdown(markdown) if markdown else None


def _arxiv_fulltext_available(url: str) -> bool:
    """轻量版可用性探测：只确认 arxiv 全文 HTML 端点已经渲染出真实正文，不下载/处理
    配图、不做 markdown 清洗——供 check_arxiv_fulltext_activity 用。真正就绪的候选会
    交给 EnrichArticleWorkflow 走 `_try_arxiv_fulltext` 完整抓取（届时才值得付出下载/
    处理配图的成本），这里只用 trafilatura 快速判断页面是否已经有实际内容，成功判定
    标准跟 `_try_arxiv_fulltext` 保持一致（只看 markdown 是否非空，不引入新的长度
    阈值，避免两边判定口径不一致导致"探测说就绪、真抓却还没渲染完"这类边界不一致）。
    """
    fulltext_url = _arxiv_fulltext_url(url)
    if not fulltext_url:
        return False
    try:
        response = _safe_get(fulltext_url, headers={"User-Agent": USER_AGENT}, timeout=30.0)
    except (httpx.HTTPError, SSRFBlockedError):
        return False
    if response.status_code != 200:
        return False
    markdown = trafilatura.extract(response.text, url=fulltext_url, output_format="markdown", with_metadata=False)
    return bool(markdown)


@activity.defn
def check_arxiv_fulltext_activity(url: str) -> bool:
    """`worker/arxiv_backfill.py` 专用：只做一次轻量 HTTP 请求判断"这篇文章现在有没有
    全文了"，不含任何 LLM 调用、不下载配图——先用这个便宜的检查筛掉仍然只有摘要的
    候选，避免对每天都还没等到全文的文章重复浪费翻译/摘要/元数据这几个 LLM 调用
    （2026-07-08 修复：此前直接复用 `_try_arxiv_fulltext`，等于对每个候选都完整跑了
    一遍配图下载+trafilatura 抽取的真实抓取开销，结果只取一个 bool 就整体丢弃，真正
    就绪的候选紧接着还要被 EnrichArticleWorkflow 完整重新抓一遍，全文 HTML 和每张
    配图都被下载了两次）。"""
    return _arxiv_fulltext_available(url)


def _fetch_direct(url: str) -> tuple[str | None, bool]:
    """状态①：direct 通道，唯一会处理配图的通道。命中 Jina 触发条件（含重定向跳到
    不安全地址，按 SSRF 拦截处理）时返回 None 交给调用方走兜底；其余 HTTP 错误（如真实
    的 404）原样抛出——由调用方 fetch_original_activity 统一捕获并降级到下一通道，
    这里不吞掉，方便日志里看到具体是哪种错误。

    arxiv 来源会先尝试全文 HTML 端点（见 `_try_arxiv_fulltext`），命中就直接返回；
    未命中（非 arxiv 来源，或 arxiv 全文暂不可用）走下面原有的摘要页/原页面抓取逻辑，
    行为与此前完全一致。返回值第二项标记"这次是否命中了 arxiv 全文"——供
    fetch_original_activity 计算 `arxiv_fulltext_pending`（2026-07-08 新增，供每日
    arxiv 全文回补 workflow 查询候选，见 worker/arxiv_backfill.py）。
    """
    fulltext = _try_arxiv_fulltext(url)
    if fulltext:
        return fulltext, True

    try:
        response = _safe_get(url, headers={"User-Agent": USER_AGENT}, timeout=30.0)
    except (httpx.TimeoutException, SSRFBlockedError):
        return None, False
    if response.status_code in _JINA_TRIGGER_STATUS:
        return None, False
    response.raise_for_status()

    article_key = hashlib.sha256(url.encode("utf-8")).hexdigest()[:12]
    html_with_images_processed = _process_images(response.text, base_url=url, article_key=article_key)
    # include_images 默认 False，trafilatura 会连同我们改写好的本地图片引用/占位块一起
    # 丢弃——必须显式打开，否则配图分级渲染的结果全部消失（M4 实测踩过的坑）。
    markdown = trafilatura.extract(
        html_with_images_processed, url=url, output_format="markdown", with_metadata=False, include_images=True
    )
    if not markdown:
        return markdown, False
    # arxiv 摘要页专属清洗必须先于通用管线执行：_clean_arxiv_abs_markdown 的正则依赖
    # "字符串仍以原始的 # 分类标题开头"这个结构（一次性连着剥掉分类标题+提交日期+
    # Title 行），如果先跑通用管线（_strip_leading_title_h1 会把开头 H1 剥掉），这段
    # 结构被破坏，正则整体匹配失败，[Submitted...]/Title:/View PDFAbstract: 这些样板
    # 文字会原样残留进正文（2026-07-08 真实踩过的回归，通用清洗管线泛化时引入）。
    if _ARXIV_ABS_RE.match(url.strip()):
        markdown = _clean_arxiv_abs_markdown(markdown)
    markdown = _clean_fetched_markdown(markdown)
    return markdown, False


def _fetch_via_jina(url: str) -> str | None:
    """状态②：Jina Reader 兜底，直接返回已抽取好的 markdown 正文，跳过 trafilatura 转换、
    不下载图片。请求打给 Jina 自己的服务器（不在我们的内网），这里只校验 Jina 端点本身。
    """
    response = _safe_get(_JINA_READER_BASE + url, timeout=_JINA_TIMEOUT_SECONDS)
    response.raise_for_status()
    marker = "Markdown Content:"
    idx = response.text.find(marker)
    if idx == -1:
        return None
    body = response.text[idx + len(marker):].strip()
    return _clean_fetched_markdown(body) if body else None


def _fetch_via_playwright(url: str) -> str | None:
    """状态③ Fallback A：direct + Jina 都失败后，用无头浏览器渲染兜底（能过部分 JS
    挑战/反爬，纯 HTTP 客户端做不到）。不下载图片，图片保留原始外链。

    注意：这里的无头浏览器和 temporal-worker 跑在同一个 Docker 网络里，SSRF 风险
    比纯 HTTP 客户端更高（能执行 JS、发起任意子请求）。已校验入口 URL，但 Playwright
    内部导航发生的重定向/子请求不会逐跳重新校验——如果需要更强保证，后续可以用
    Playwright 的请求拦截 API（page.route）逐请求校验，这里先做入口校验这一层。
    """
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            try:
                page = browser.new_page(user_agent=USER_AGENT)
                page.goto(url, timeout=_PLAYWRIGHT_TIMEOUT_MS, wait_until="domcontentloaded")
                html = page.content()
            finally:
                browser.close()
    except PlaywrightError:
        return None
    # 不下载图片，但要保留原始外链（04 §2.4：状态③"图片保留原始外链"），
    # 同样需要 include_images=True，否则 trafilatura 直接把图片引用丢光。
    markdown = trafilatura.extract(html, url=url, output_format="markdown", with_metadata=False, include_images=True)
    if not markdown:
        return markdown
    return _clean_fetched_markdown(markdown)


def _build_placeholder_body(url: str) -> str:
    """Fallback B：全部抓取通道失败，仍需完整写入这条记录（保证下游双链引用不断链，04 §2.4）。"""
    return f"（全部抓取通道均失败，无法获取原文正文，请通过原文链接查阅：{url}）"


def _fetch_via_jina_channel(url: str) -> tuple[str | None, bool]:
    return _fetch_via_jina(url), False


def _fetch_via_playwright_channel(url: str) -> tuple[str | None, bool]:
    return _fetch_via_playwright(url), False


# 三个通道统一返回 (markdown, got_arxiv_fulltext) 二元组——只有 direct 通道会真正
# 命中 arxiv 全文端点，jina/playwright 固定传 False，但形状保持一致，调用方不需要
# 再按通道名字字符串特判返回值该怎么拆包（2026-07-08 修复：此前 jina/playwright 仍
# 返回裸字符串，fetch_original_activity 里得靠 `if channel == "direct"` 才能正确拆包）。
_FETCH_CHANNELS = (
    (_fetch_direct, "direct"),
    (_fetch_via_jina_channel, "jina"),
    (_fetch_via_playwright_channel, "playwright"),
)


def _arxiv_fulltext_pending(is_arxiv: bool, got_arxiv_fulltext: bool = False) -> bool | None:
    """三处调用（SSRF 拦截 / 抓取成功 / 全部通道失败）统一走这一个判定，避免规则
    分散在三处、改一处漏一处（2026-07-08 重构）。"""
    if not is_arxiv:
        return None
    return not got_arxiv_fulltext


@activity.defn
def fetch_original_activity(url: str) -> dict:
    """状态①→②→③依次兜底，全部失败则走 Fallback B 占位（04 §2.4，不再抛异常——
    保证 originalize 覆盖率 100% 是 M4 验收标准的核心，Fallback B 本身就是"完成"）。

    入口 URL 来自外部信息源内容，先做 SSRF 校验；命中就直接占位，不浪费任何通道去碰
    这个地址（Jina/Playwright 都可能被当成打内网的跳板）。

    真实的 404/超时/限流这类 HTTP 错误在新闻聚合场景下很常见，此前 _fetch_direct/
    _fetch_via_jina 遇到这类错误会直接抛出、穿透整个 activity 导致文章从批次消失——
    这里统一捕获，当作"这个通道失败"处理并继续尝试下一通道，而不是让异常中断整个
    降级链条。

    返回值里的 `arxiv_fulltext_pending`（仅 arxiv 摘要页 URL 才有意义，非 arxiv 为
    None）标记"这篇文章这次有没有拿到 arxiv 全文"——`_try_arxiv_fulltext` 未命中时
    为 True，供每日 arxiv 全文回补 workflow（`worker/arxiv_backfill.py`）查询候选。
    """
    is_arxiv = _arxiv_fulltext_url(url) is not None

    try:
        _assert_public_url(url)
    except SSRFBlockedError as exc:
        activity.logger.warning(f"拒绝抓取不安全的 URL，直接写占位正文：{exc}")
        return {
            "body_md": _build_placeholder_body(url),
            "fetch_channel": "placeholder",
            "arxiv_fulltext_pending": _arxiv_fulltext_pending(is_arxiv),
        }

    for fetch_fn, channel in _FETCH_CHANNELS:
        try:
            body_md, got_arxiv_fulltext = fetch_fn(url)
        except httpx.HTTPError as exc:
            activity.logger.warning(f"{channel} 通道抓取失败（{exc!r}），尝试下一通道：{url}")
            continue
        if body_md:
            return {
                "body_md": body_md,
                "fetch_channel": channel,
                "arxiv_fulltext_pending": _arxiv_fulltext_pending(is_arxiv, got_arxiv_fulltext),
            }

    activity.logger.warning(f"direct/jina/playwright 全部抓取通道失败，写入占位正文：{url}")
    return {
        "body_md": _build_placeholder_body(url),
        "fetch_channel": "placeholder",
        "arxiv_fulltext_pending": _arxiv_fulltext_pending(is_arxiv),
    }


# ---------------------------------------------------------------------------
# 翻译 + 完整性机械校验
# ---------------------------------------------------------------------------

def _strip_code_and_links(text: str) -> str:
    """代码块/URL 不算翻译内容，不应计入 CJK 占比分母——本地图片自定义 scheme
    （ainews-media://，含随机哈希文件名）之前漏了，短小的配图说明块会被这串哈希
    拖累占比（诊断真实批次数据后发现）。"""
    text = re.sub(r"```.*?```", "", text, flags=re.DOTALL)
    text = re.sub(r"https?://\S+", "", text)
    return re.sub(re.escape(IMAGE_URL_PREFIX) + r"\S+", "", text)


# 判断一行是不是"数据行/图表提取噪声"（标识符+数字表格、图表转 Markdown 产生的字符画），
# 而非需要翻译的自然语言正文。判据：这一行里找不到长度>=3 的连续拉丁字母、或长度>=2 的
# 连续中日韩文字——真正的自然语言句子几乎不可能连一个这样的片段都没有（诊断真实批次数据后
# 发现：Genebench-Pro 类数据表格行、Jina Reader 把图表转成的数字字符画都符合这个特征）。
_WORD_LIKE_RE = re.compile(r"[A-Za-z]{3,}|[一-鿿]{2,}")


def _is_data_or_noise_line(line: str) -> bool:
    stripped = line.strip()
    if not stripped:
        return False
    return _WORD_LIKE_RE.search(stripped) is None


def _strip_data_and_noise_lines(text: str) -> str:
    kept = [line for line in text.split("\n") if not _is_data_or_noise_line(line)]
    return "\n".join(kept)


# 只有几乎整块都是噪声（≥95% 行）才跳过翻译——数据表格常见"表头(可翻译)+大量数据行"混合
# 结构，阈值定太低会连表头一起漏翻，实际收益不大（表头对应的 CJK 占比稀释问题已经靠
# _strip_data_and_noise_lines 在完整性校验里解决，不需要靠跳过翻译额外兜底）。
_MOSTLY_NOISE_LINE_RATIO = 0.95


def _is_mostly_noise(text: str) -> bool:
    """整块内容几乎全是数据表格/图表提取噪声（如 Jina Reader 把图表转成的数字字符画），
    没有实质可翻译的自然语言——跳过翻译调用，原样保留（04 §2.4 诊断实测发现：这类内容
    不是翻译模型的问题，硬翻只会浪费 token、甚至让模型编造译文）。
    """
    lines = [line for line in text.split("\n") if line.strip()]
    if not lines:
        return False
    noise_count = sum(1 for line in lines if _is_data_or_noise_line(line))
    return noise_count / len(lines) >= _MOSTLY_NOISE_LINE_RATIO


def _cjk_ratio_excluding_code(text: str) -> float:
    stripped = _strip_code_and_links(text)
    stripped = _strip_data_and_noise_lines(stripped)
    total = len(re.sub(r"\s", "", stripped))
    if total == 0:
        return 0.0
    return sum(1 for ch in stripped if _is_cjk_char(ch)) / total


def _has_untranslated_residue(text: str) -> bool:
    return any(p.search(text) for p in _UNTRANSLATED_RESIDUE_PATTERNS)


def _matched_residue_pattern(text: str) -> str | None:
    for p in _UNTRANSLATED_RESIDUE_PATTERNS:
        if p.search(text):
            return p.pattern
    return None


def _validate_translation_completeness(translated_body: str) -> bool:
    """04 §2.4 硬约束：翻译完成后机械检测非中文占比/HTML-LaTeX 残留，不能只凭主观判断。"""
    if _has_untranslated_residue(translated_body):
        return False
    return _cjk_ratio_excluding_code(translated_body) >= _TRANSLATION_CJK_RATIO_THRESHOLD


# 单个分块译文 CJK 占比低于这个门槛，视为"模型几乎没翻译"（诊断真实批次数据确认：
# 真正的翻译失败案例占比恒为 0.00-0.03，合法的数据密集内容排除噪声行后占比明显更高），
# 带纠错提示重试一次。
_CHUNK_RETRY_CJK_THRESHOLD = 0.10

_TRANSLATE_SYSTEM_PROMPT = (
    "你是专业的技术文章翻译助手。把下面这段 Markdown 正文翻译成中文："
    "逐段对应，不合并、不总结、不评论；保留标题层级/代码块/公式/引用块等 Markdown 结构；"
    "专有名词保留原文并在首次出现时用括号给出中文解释；保留数字精确度。"
)

_TRANSLATE_RETRY_SYSTEM_PROMPT = _TRANSLATE_SYSTEM_PROMPT + (
    "\n注意：你上一次的输出几乎完全保留了英文原文，没有实际翻译。这次必须把所有自然语言正文"
    "完整译成中文，只允许专有名词、代码块、公式、URL、纯数据/表格行保留原文。"
)


def _translate_chunk(
    chunk: str, *, retry: bool = False, model: str = TRANSLATE_MODEL, max_tokens: int = DEFAULT_MAX_TOKENS
) -> str:
    result = call_structured(
        model=model,
        system_prompt=_TRANSLATE_RETRY_SYSTEM_PROMPT if retry else _TRANSLATE_SYSTEM_PROMPT,
        user_content=chunk,
        response_model=ChunkTranslation,
        max_tokens=max_tokens,
    )
    return result.translated_text


_CHUNK_MAX_RETRIES = 2

# 2026-07-08：主模型 + 换模型兜底都失败后的第三层兜底——对半切开分别翻译再拼接，
# 递归直到低于这个下限就放弃细分（不是"切了也没用"，是避免极端情况下无限切分）。
_MIN_SPLIT_CHARS = 400

_TABLE_ROW_RE = re.compile(r"^\s*\|.*\|\s*$")
_CODE_FENCE_RE = re.compile(r"^\s*```")


def _atomic_blocks(text: str) -> list[str]:
    """按行扫描，把表格连续行、代码块内部行分别合并成不可拆分的原子块，其余行按
    空行分段——切分候选点只能落在原子块之间，绝不能切进表格或代码块内部（切在表格
    中间会导致两半各自都不是合法的表格片段，模型翻译/拼接后结构必然错乱，比保留
    原文更糟）。"""
    lines = text.split("\n")
    blocks: list[str] = []
    buf: list[str] = []
    in_code = False
    in_table = False

    def flush():
        if buf:
            blocks.append("\n".join(buf))
            buf.clear()

    for line in lines:
        if _CODE_FENCE_RE.match(line):
            buf.append(line)
            if in_code:
                flush()
            in_code = not in_code
            continue
        if in_code:
            buf.append(line)
            continue

        is_table_row = bool(_TABLE_ROW_RE.match(line))
        if is_table_row:
            if not in_table:
                flush()
                in_table = True
            buf.append(line)
            continue
        if in_table:
            flush()
            in_table = False
        buf.append(line)
        if line.strip() == "":
            flush()

    flush()
    return [b for b in blocks if b.strip()]


def _split_chunk_at_safe_boundary(chunk: str) -> list[str] | None:
    """在原子块边界把一个分块尽量均衡地切成两半；如果整个分块本质上只有一个不可拆的
    原子块（比如从头到尾都是同一张大表/同一段代码），返回 None——调用方必须放弃细分，
    宁可让这一整块保留原文，也不能冒险切坏表格/代码结构。"""
    blocks = _atomic_blocks(chunk)
    if len(blocks) < 2:
        return None

    sizes = [len(b) for b in blocks]
    target = sum(sizes) / 2
    acc = 0
    split_at = len(blocks) // 2
    for i, size in enumerate(sizes):
        acc += size
        if acc >= target:
            split_at = i + 1
            break

    split_at = max(1, min(split_at, len(blocks) - 1))
    left = "\n".join(blocks[:split_at]).strip()
    right = "\n".join(blocks[split_at:]).strip()
    if not left or not right:
        return None
    return [left, right]


def _translate_oversized_chunk(chunk: str, cause: Exception) -> tuple[str, bool]:
    """策略链（主模型+换模型兜底）都失败后调用：尝试在安全边界切成两半分别翻译再
    拼接，递归直到低于 `_MIN_SPLIT_CHARS` 或找不到安全切点为止。返回 (译文, 是否仍
    有残留未翻译的部分)——即使拆分后大部分内容翻译成功，只要有任何一小块最终仍
    保留原文，也要如实上报，不能因为"大部分翻好了"就悄悄当作完全成功（04 §2.4
    硬约束）。切开的每一半直接用 _TRANSLATE_FALLBACK_MODEL（已经证明比主模型更能
    处理这类内容），不再退回已知会在这块内容上失败的主模型（2026-07-08 修复：此前
    这里调用不带 model 参数的 _translate_chunk，默认落回主模型，等于对已知失败的
    内容重复发起注定失败的调用）。"""
    if len(chunk) <= _MIN_SPLIT_CHARS:
        activity.logger.warning(f"[chunk_diag] 分块已达细分下限仍失败，保留原文：{cause!r}")
        return chunk, True

    halves = _split_chunk_at_safe_boundary(chunk)
    if halves is None:
        activity.logger.warning(f"[chunk_diag] 分块整体是不可拆的原子块（如单张大表/单段代码），保留原文：{cause!r}")
        return chunk, True

    translated_parts = []
    had_failure = False
    for half in halves:
        try:
            translated_parts.append(
                _translate_chunk(half, model=_TRANSLATE_FALLBACK_MODEL, max_tokens=_TRANSLATE_FALLBACK_MAX_TOKENS)
            )
        except (IncompleteOutputException, InstructorRetryException) as half_exc:
            part_text, part_failed = _translate_oversized_chunk(half, half_exc)
            translated_parts.append(part_text)
            had_failure = had_failure or part_failed
    return "\n\n".join(translated_parts), had_failure

# 分块翻译并发上限：分块彼此独立、各自一次网络请求，线程池并发是安全的加速手段；
# 但 Temporal 本身已经按文章级别做 fan-out（activity_executor 最多 20 个并发 activity，
# worker.py::MAX_ACTIVITY_WORKERS），单篇内部再叠加并发是"乘法"而不是"加法"——
# 6 意味着最多 20×6=120 路并发同时打向同一个共享 LiteLLM 客户端。2026-07-06 排查
# aggregate_activity 反复超时问题时一度怀疑是这里的高并发拖垮了共享连接池，但用
# py-spy 栈追踪+完整日志核查确认真正根因是另一件事（gRPC 4MB 消息上限，见
# .claude/memory/decisions.md「M7 观察期：aggregate_activity/write_activity 合并
# 修复 gRPC 4MB 消息上限」），跟这里的并发数无关。降到 2（乘法上限压到 20×2=40）
# 仍然保留——单纯是"不过度放大对自建网关的瞬时并发冲击"这个独立考量，不是在修复
# aggregate_activity 超时。
_CHUNK_TRANSLATE_CONCURRENCY = 2

# 分块翻译策略链：依次尝试的 (model, max_tokens) 组合，全部失败才进入安全切分兜底
# （_translate_oversized_chunk）。以后再加一层兜底模型只需要在这里追加一项，不需要
# 再嵌套一层 try/except（2026-07-08 重构，原先是两层嵌套的 if/except）。
_TRANSLATE_STRATEGIES: list[tuple[str, int]] = [
    (TRANSLATE_MODEL, DEFAULT_MAX_TOKENS),
    (_TRANSLATE_FALLBACK_MODEL, _TRANSLATE_FALLBACK_MAX_TOKENS),
]


class ChunkTranslateResult(NamedTuple):
    """单个分块的翻译结果。text：最终译文；skipped_as_noise：是否判定为图表/数据
    噪声直接跳过翻译；retry_count：CJK 复检循环实际重试次数；had_residual_failure：
    安全切分兜底用尽后是否仍有残留部分保留了原文。"""

    text: str
    skipped_as_noise: bool
    retry_count: int
    had_residual_failure: bool


def _translate_chunk_with_retry(chunk: str) -> ChunkTranslateResult:
    """依次尝试 _TRANSLATE_STRATEGIES 策略链，全部失败再走安全切分兜底；成功后做一次
    CJK 占比复检重试——最多重试两次（而非一次）：实测发现同一分块独立重跑结果会
    摆动（模型偶发漏译开头一句话，换一次采样就正常了），多一次重试能吃掉大部分这类
    纯随机性失败，而不需要放宽 CJK 占比阈值本身（放宽阈值会连真正的翻译缺失一起
    放过）。

    复检重试固定复用"策略链里刚刚成功的那个模型"，且本身包了 try/except（2026-07-08
    修复：此前复检循环无条件用默认主模型重试且没有异常保护——如果策略链最终生效的
    是换模型兜底或安全切分兜底，复检会对已经验证会失败的模型/内容再发起一次无保护
    调用，真实跑出过把刚成功的译文整体丢弃、退化成保留原文的情况）。
    """
    if _is_mostly_noise(chunk):
        return ChunkTranslateResult(chunk, True, 0, False)

    translated = ""
    effective_model = _TRANSLATE_STRATEGIES[0][0]
    last_exc: Exception | None = None
    for model, max_tokens in _TRANSLATE_STRATEGIES:
        try:
            translated = _translate_chunk(chunk, model=model, max_tokens=max_tokens)
            effective_model = model
            last_exc = None
            break
        except (IncompleteOutputException, InstructorRetryException) as exc:
            activity.logger.warning(f"[chunk_diag] {model} 翻译调用异常：{exc!r}")
            last_exc = exc

    had_residual_failure = False
    if last_exc is not None:
        # 策略链全部失败，大概率是这块内容本身密度太高撞了 token 预算，不是模型能力
        # 问题——尝试在安全边界切开分别翻译再拼接，而不是直接放弃整块保留原文。
        activity.logger.warning(f"[chunk_diag] 翻译策略链全部失败，尝试安全切分后分别翻译：{last_exc!r}")
        translated, had_residual_failure = _translate_oversized_chunk(chunk, last_exc)
        effective_model = _TRANSLATE_FALLBACK_MODEL

    retry_count = 0
    if not had_residual_failure:
        while _cjk_ratio_excluding_code(translated) < _CHUNK_RETRY_CJK_THRESHOLD and retry_count < _CHUNK_MAX_RETRIES:
            try:
                translated = _translate_chunk(chunk, retry=True, model=effective_model)
            except (IncompleteOutputException, InstructorRetryException) as exc:
                # 复检重试本身失败，保留复检前已经拿到的译文，不再无保护地冒泡异常。
                activity.logger.warning(f"[chunk_diag] CJK 复检重试调用异常，保留复检前译文：{exc!r}")
                break
            retry_count += 1
    return ChunkTranslateResult(translated, False, retry_count, had_residual_failure)


def _review_translation_completeness(original_body: str, translated_body: str) -> bool:
    """机械 CJK 占比校验未通过时的独立复审：不是让翻译模型自证"我翻完了"（04 §2.4
    硬约束明确禁止的自估），而是另开一次调用，对照原文逐段核对译文是否完整——机械
    校验继续作为主判据，这一步只用来减少专有名词/数据表格密度天然偏高导致的误杀。
    """
    result = call_structured(
        model=TRANSLATE_MODEL,
        system_prompt=(
            "你是翻译质检员。下面会给你一篇英文原文和对应的中文译文，你的任务是对照原文逐段核对，"
            "判断译文是否完整传达了原文的全部信息。译文里专有名词/品牌名/代码/公式/URL/数据表格行"
            "保留英文或数字原样是正常的，不算翻译缺失；但如果大段自然语言正文被原样跳过没有翻译，"
            "或明显遗漏了原文的实质内容，就判定为不完整。"
        ),
        user_content=f"【原文】\n{original_body}\n\n【译文】\n{translated_body}",
        response_model=TranslationCompletenessReview,
    )
    return result.is_complete


@activity.defn
def translate_activity(title: str, body_md: str) -> dict:
    """标题单独翻译一次；正文按段落分块逐块翻译再拼接；拼接后做机械完整性校验，
    校验不过则走唯一允许的降级路径（保留首尾分块+中间占位说明，04 §2.4 硬约束）。

    三处针对性处理（诊断真实批次数据后新增，见 decisions.md）：
    - 疑似图表/数据噪声的分块跳过翻译调用；
    - 单个分块译文 CJK 占比过低（几乎没翻译）时，带纠错提示最多重试两次；
    - 机械校验（CJK 占比）未通过时，用独立复审判断是不是专有名词/数据密度导致的
      误杀——机械校验仍是唯一的主判据，复审只是减少误杀，不改变"不能自估"的硬约束。
    """
    title_result = call_structured(
        model=TRANSLATE_MODEL,
        system_prompt="你是专业的技术文章翻译助手，将标题翻译成中文，专有名词保留原文并在括号内给出中文解释。",
        user_content=title,
        response_model=TitleTranslation,
    )

    chunks = _chunk_paragraphs(body_md)
    translated_chunks: list[str | None] = [None] * len(chunks)
    failed_chunk_indices: list[int] = []

    def _translate_one(i: int, chunk: str) -> None:
        try:
            translated, skipped_as_noise, retry_count, had_residual_failure = _translate_chunk_with_retry(chunk)
        except Exception as exc:  # noqa: BLE001 - 单个分块的翻译调用异常（三层兜底——换模型/
            # 安全切分——全部失败才会走到这里）不该打断整篇文章的翻译；保留原文分块，
            # 明确记为降级，而不是让整个 activity 崩溃、这篇文章从批次里彻底消失。
            activity.logger.warning(
                f"[chunk_diag] title={title[:40]!r} chunk={i}/{len(chunks)} 翻译调用异常，保留原文：{exc!r}"
            )
            translated_chunks[i] = chunk
            failed_chunk_indices.append(i)
            return

        translated_chunks[i] = translated
        if had_residual_failure:
            # 安全切分兜底翻完了大部分内容，但递归到底仍有一小块保留原文——不能因为
            # "大部分翻好了"就悄悄当作完全成功，如实计入失败分块（04 §2.4 硬约束）。
            failed_chunk_indices.append(i)

        # 诊断专用：逐块记录 CJK 占比/残留命中情况，用于精确定位整体校验失败的根因，
        # 附带记录本块是否被跳过翻译/实际重试了几次，方便评估修复效果。
        ratio = _cjk_ratio_excluding_code(translated)
        residue = _matched_residue_pattern(translated)
        if residue or ratio < _TRANSLATION_CJK_RATIO_THRESHOLD:
            activity.logger.warning(
                f"[chunk_diag] title={title[:40]!r} chunk={i}/{len(chunks)} "
                f"cjk_ratio={ratio:.2f} residue={residue} chunk_len={len(translated)} "
                f"skipped_as_noise={skipped_as_noise} retry_count={retry_count} "
                f"preview={translated[:80]!r}"
            )

    # 分块之间彼此独立（互不依赖上下文），并发翻译——此前逐块顺序调用，全文版 arxiv
    # 论文常见 30-50 个分块，单篇光翻译就要十几分钟，超出 workflows.py 给这个 activity
    # 配置的 start_to_close_timeout（真实批次实测踩到过）。每个分块各自独立起一次
    # call_structured 网络请求，线程池并发对 I/O 密集型调用是安全且有效的加速手段；
    # `_CHUNK_TRANSLATE_CONCURRENCY` 控制并发上限，避免瞬间打满网关。
    #
    # 2026-07-07：按提交顺序 for future in futures 等待时，futures[0] 没完成前不会看到
    # futures[5]（可能早就完成了）的进度——心跳必须按"实际完成顺序"上报才能反映真实
    # 进度，否则超大论文（100+ 分块）翻译到一半也会显得"很久没心跳"。改用 as_completed
    # 逐块上报心跳，配合 workflows.py 新增的 heartbeat_timeout，让"真卡死"能被快速
    # 发现，同时不再需要靠不断调大固定 start_to_close_timeout 硬顶超大论文。
    with ThreadPoolExecutor(max_workers=min(_CHUNK_TRANSLATE_CONCURRENCY, len(chunks) or 1)) as pool:
        futures = {pool.submit(_translate_one, i, chunk): i for i, chunk in enumerate(chunks)}
        done_count = 0
        for future in as_completed(futures):
            future.result()  # _translate_one 内部已捕获单块翻译异常，这里正常不会再抛出
            done_count += 1
            activity.heartbeat(f"{done_count}/{len(chunks)} 个分块翻译完成")

    translated_body = "\n\n".join(translated_chunks)

    # 单块翻译异常已经在 _translate_one 里降级为"保留原文"，但不能因此悄悄放过——
    # 04 §2.4 硬约束是"不能悄悄标记为翻译完成"，即使失败的分块只占少数、拼接后的
    # 整体 CJK 占比仍然达标（不会触发下面的完整性校验降级），也必须显式记一条通知。
    fallback_notice = None
    if failed_chunk_indices:
        fallback_notice = f"{len(failed_chunk_indices)}/{len(chunks)} 个分块因翻译调用异常保留原文未翻译"

    if not _validate_translation_completeness(translated_body):
        has_residue = _has_untranslated_residue(translated_body)
        reviewed_as_complete = False
        if not has_residue:
            # 复审只处理"CJK 占比不达标"这一种触发原因——HTML/LaTeX 残留基本不会
            # 误判（诊断真实批次数据：93 条分块诊断 residue 命中率为 0），没必要复审。
            reviewed_as_complete = _review_translation_completeness(body_md, translated_body)
        if reviewed_as_complete:
            activity.logger.warning(
                f"translate_activity 机械校验未通过但复审判定翻译完整（专有名词/数据密度导致误判）：{title[:50]}"
            )
        else:
            if len(translated_chunks) > 2:
                # 唯一允许的降级路径：保留首尾分块（近似摘要/引言 + 结论）完整翻译，
                # 中间占位说明——绝不悄悄标记为翻译完成（04 §2.4 硬约束）。
                translated_body = "\n\n".join(
                    [
                        translated_chunks[0],
                        "（原文中间部分因翻译完整性校验未通过，未逐段翻译，可通过原文链接查阅完整内容）",
                        translated_chunks[-1],
                    ]
                )
            completeness_notice = "翻译完整性机械校验未通过，仅完整翻译首尾部分，其余章节保留原文提示"
            fallback_notice = f"{fallback_notice}；{completeness_notice}" if fallback_notice else completeness_notice
            activity.logger.warning(f"translate_activity 完整性校验未通过：{title[:50]}")

    return {
        "translated_title": title_result.translated_title,
        "translated_body_md": translated_body,
        "translation_fallback_notice": fallback_notice,
    }


@activity.defn
def gist_activity(title: str, body_md: str) -> str:
    """一段话摘要，基于保证是中文的标题+正文生成。gist 是 Daily TL;DR/Daily 主题条目/
    Digest blurb/Deep Dive 代表文章行共用的唯一摘要来源（04 §2.5），加粗指令只需要在
    这一处生成时给一次，就能让重点在全部下游消费点上生效，不需要各自单独处理。"""
    result = call_structured(
        model=GIST_MODEL,
        system_prompt=(
            "你是新闻摘要助手，用一段中文话（80-150字）概括这篇文章讲了什么，不要逐段复述细节。"
            "每条摘要都必须用 Markdown **加粗** 标出 2-4 处最关键的信息——可以是关键词/短语"
            "（核心产品名/机构名/关键数字），也可以是一句不超过15字的核心结论/发现（如"
            "\"预测误差降低30%\"），目的是让读者快速扫描时一眼抓到重点；但不要整句话全部加粗，"
            "不要为了凑数而加粗无关紧要的内容，也不要为了加粗而生造摘要之外的内容。"
        ),
        user_content=f"{title}\n\n{body_md}",
        response_model=ArticleGist,
    )
    return result.gist


@activity.defn
def metadata_activity(title: str, body_md: str) -> dict:
    """富元数据抽取（04 §2.4）：实体/内容类型/新颖度辅助信号，只判断"这篇文章本身是
    什么"，不做跨文章判断（是否与同批次其他文章重复留给 aggregate_activity）。
    """
    result = call_structured(
        model=METADATA_MODEL,
        system_prompt=(
            "你是文章元数据抽取助手。只根据这篇文章本身的内容抽取信息，不要判断它和其他"
            "文章的关系（是否重复/该归哪个话题不归你判断）。"
        ),
        user_content=f"{title}\n\n{body_md}",
        response_model=ArticleMetadata,
    )
    return {
        "entities": result.entities,
        "content_type": result.content_type,
        "novelty_keywords": result.novelty_keywords,
    }


def _compute_word_count(body_md: str) -> int:
    """机械计算正文字数（04 §2.4 硬约束：不能靠 LLM 自估，旧系统偏差最高达 20-30 倍）。
    口径与 _strip_code_and_links 一致：代码块/URL/本地图片引用不算正文字数。
    """
    stripped = _strip_code_and_links(body_md)
    return len(re.sub(r"\s", "", stripped))


@activity.defn
def upsert_article_activity(payload: dict) -> None:
    upsert_enriched_article(**payload)


def content_hash(body_md: str) -> str:
    """[Temporal 回放安全] 同 needs_translation：workflows.py 直接调用，必须保持纯函数。"""
    return hashlib.sha256(body_md.encode("utf-8")).hexdigest()


def compute_word_count(body_md: str) -> int:
    """[Temporal 回放安全] 同 needs_translation：workflows.py 直接调用，必须保持纯函数。"""
    return _compute_word_count(body_md)
