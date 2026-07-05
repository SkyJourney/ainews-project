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
from datetime import date
from pathlib import Path
from urllib.parse import urljoin, urlparse

import httpx
import trafilatura
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import sync_playwright
from temporalio import activity

from worker.db import upsert_enriched_article
from worker.llm_client import call_structured
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
_ICON_SRC_PATTERNS = [re.compile(r"/static/browse/.*?/icons/", re.IGNORECASE)]
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
    """按段落贪心分块，避免长文本单次撑爆上下文（用户在 M1 明确要求的分段翻译）。"""
    paragraphs = body_md.split("\n\n")
    chunks: list[str] = []
    current: list[str] = []
    current_len = 0

    for para in paragraphs:
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


def _clean_fetched_markdown(markdown: str) -> str:
    """三个抓取通道共用的最终 markdown 清洗管线，按顺序：已知区块截断 → 站点头部
    导航剥离 → 跟踪像素引用清理 → 整段重复内容去重。"""
    markdown = _strip_known_boilerplate(markdown)
    markdown = _strip_openai_header_nav(markdown)
    markdown = _strip_tracking_pixel_refs(markdown)
    return _dedup_repeated_paragraphs(markdown)


def _fetch_direct(url: str) -> str | None:
    """状态①：direct 通道，唯一会处理配图的通道。命中 Jina 触发条件（含重定向跳到
    不安全地址，按 SSRF 拦截处理）时返回 None 交给调用方走兜底；其余 HTTP 错误（如真实
    的 404）原样抛出——由调用方 fetch_original_activity 统一捕获并降级到下一通道，
    这里不吞掉，方便日志里看到具体是哪种错误。
    """
    try:
        response = _safe_get(url, headers={"User-Agent": USER_AGENT}, timeout=30.0)
    except (httpx.TimeoutException, SSRFBlockedError):
        return None
    if response.status_code in _JINA_TRIGGER_STATUS:
        return None
    response.raise_for_status()

    article_key = hashlib.sha256(url.encode("utf-8")).hexdigest()[:12]
    html_with_images_processed = _process_images(response.text, base_url=url, article_key=article_key)
    # include_images 默认 False，trafilatura 会连同我们改写好的本地图片引用/占位块一起
    # 丢弃——必须显式打开，否则配图分级渲染的结果全部消失（M4 实测踩过的坑）。
    markdown = trafilatura.extract(
        html_with_images_processed, url=url, output_format="markdown", with_metadata=False, include_images=True
    )
    if not markdown:
        return markdown
    return _clean_fetched_markdown(markdown)


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


_FETCH_CHANNELS = (
    (_fetch_direct, "direct"),
    (_fetch_via_jina, "jina"),
    (_fetch_via_playwright, "playwright"),
)


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
    """
    try:
        _assert_public_url(url)
    except SSRFBlockedError as exc:
        activity.logger.warning(f"拒绝抓取不安全的 URL，直接写占位正文：{exc}")
        return {"body_md": _build_placeholder_body(url), "fetch_channel": "placeholder"}

    for fetch_fn, channel in _FETCH_CHANNELS:
        try:
            body_md = fetch_fn(url)
        except httpx.HTTPError as exc:
            activity.logger.warning(f"{channel} 通道抓取失败（{exc!r}），尝试下一通道：{url}")
            continue
        if body_md:
            return {"body_md": body_md, "fetch_channel": channel}

    activity.logger.warning(f"direct/jina/playwright 全部抓取通道失败，写入占位正文：{url}")
    return {"body_md": _build_placeholder_body(url), "fetch_channel": "placeholder"}


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


def _translate_chunk(chunk: str, *, retry: bool = False) -> str:
    result = call_structured(
        model=TRANSLATE_MODEL,
        system_prompt=_TRANSLATE_RETRY_SYSTEM_PROMPT if retry else _TRANSLATE_SYSTEM_PROMPT,
        user_content=chunk,
        response_model=ChunkTranslation,
    )
    return result.translated_text


_CHUNK_MAX_RETRIES = 2


def _translate_chunk_with_retry(chunk: str) -> tuple[str, bool, int]:
    """返回 (译文, 是否判定为噪声跳过翻译, 实际重试次数)。

    最多重试两次（而非一次）：实测发现同一分块独立重跑结果会摆动（模型偶发漏译
    开头一句话，换一次采样就正常了），多一次重试能吃掉大部分这类纯随机性失败，
    而不需要放宽 CJK 占比阈值本身（放宽阈值会连真正的翻译缺失一起放过）。
    """
    if _is_mostly_noise(chunk):
        return chunk, True, 0
    translated = _translate_chunk(chunk)
    retry_count = 0
    while _cjk_ratio_excluding_code(translated) < _CHUNK_RETRY_CJK_THRESHOLD and retry_count < _CHUNK_MAX_RETRIES:
        translated = _translate_chunk(chunk, retry=True)
        retry_count += 1
    return translated, False, retry_count


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
    translated_chunks: list[str] = []
    for i, chunk in enumerate(chunks):
        translated, skipped_as_noise, retry_count = _translate_chunk_with_retry(chunk)
        translated_chunks.append(translated)

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

    translated_body = "\n\n".join(translated_chunks)

    fallback_notice = None
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
            fallback_notice = "翻译完整性机械校验未通过，仅完整翻译首尾部分，其余章节保留原文提示"
            activity.logger.warning(f"translate_activity 完整性校验未通过：{title[:50]}")

    return {
        "translated_title": title_result.translated_title,
        "translated_body_md": translated_body,
        "translation_fallback_notice": fallback_notice,
    }


@activity.defn
def gist_activity(title: str, body_md: str) -> str:
    """一段话摘要，基于保证是中文的标题+正文生成。"""
    result = call_structured(
        model=GIST_MODEL,
        system_prompt="你是新闻摘要助手，用一段中文话（80-150字）概括这篇文章讲了什么，不要逐段复述细节。",
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
