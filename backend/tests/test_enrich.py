"""enrich.py 既有纯函数回溯测试（M5 工程化收敛补项）。翻译降级深度排查阶段
（.claude/memory/decisions.md）新增的这批噪声识别/CJK 占比函数此前只在真实批次上
验证过，这里补上针对性的输入/输出单测，固化已知的判定边界。
"""

from __future__ import annotations

from worker import enrich


def test_is_data_or_noise_line_detects_numeric_chart_garbage():
    assert enrich._is_data_or_noise_line("655 655555 555 12 89") is True


def test_is_data_or_noise_line_natural_language_is_not_noise():
    assert enrich._is_data_or_noise_line("这是一句正常的中文句子。") is False
    assert enrich._is_data_or_noise_line("This is a normal English sentence.") is False


def test_is_data_or_noise_line_blank_line_is_not_noise():
    assert enrich._is_data_or_noise_line("   ") is False


def test_is_mostly_noise_requires_high_ratio():
    mostly_data = "\n".join(["1 2 3", "4 5 6", "7 8 9", "这是正文"])  # 3/4 = 75% < 95%
    assert enrich._is_mostly_noise(mostly_data) is False
    all_data = "\n".join(["1 2 3", "4 5 6", "7 8 9", "10 11 12"])
    assert enrich._is_mostly_noise(all_data) is True


def test_cjk_ratio_excluding_code_strips_code_blocks_and_image_refs():
    # 注意：_is_cjk_char 的码位范围不含中文全角标点（如"。"），这里特意不用标点收尾，
    # 断言才能精确等于 1.0，避免测试对既有 _is_cjk_char 判定边界做出错误假设。
    text = "这是中文正文内容\n```\nsome english code that should not count\n```\n" + enrich.IMAGE_URL_PREFIX + "abcdef123456.png"
    ratio = enrich._cjk_ratio_excluding_code(text)
    assert ratio == 1.0  # 排除代码块和图片引用后，剩下的全是中文


def test_cjk_ratio_excluding_code_excludes_noise_lines_from_denominator():
    text = "这是中文正文一句话\n123 456 789 000"
    ratio = enrich._cjk_ratio_excluding_code(text)
    assert ratio == 1.0  # 数据行被排除在分母之外，不会拖累占比


def test_has_untranslated_residue_detects_latex_table_markers():
    assert enrich._has_untranslated_residue('<td class="ltx_td">内容</td>') is True
    assert enrich._has_untranslated_residue("正常的纯中文正文") is False


def test_validate_translation_completeness_passes_high_cjk_ratio():
    assert enrich._validate_translation_completeness("这是一段完全翻译好的中文正文，内容详实完整。") is True


def test_validate_translation_completeness_fails_low_cjk_ratio():
    assert enrich._validate_translation_completeness("This paragraph was barely translated at all into Chinese.") is False


def test_needs_translation_false_for_already_chinese_title():
    assert enrich.needs_translation("这是中文标题", "some english body") is False


def test_needs_translation_true_for_english_content():
    assert enrich.needs_translation("English Title", "This is an English article body with no Chinese at all.") is True


# ---------------------------------------------------------------------------
# compute_word_count：机械字数统计（04 §2.4 硬约束：不能靠 LLM 自估），此前口径
# 未剥离链接/图片引用导致注水（I2，见 .claude/memory/known_issues.md），补边界测试。
# ---------------------------------------------------------------------------

def test_compute_word_count_counts_non_whitespace_chars():
    assert enrich.compute_word_count("这是五个字") == 5


def test_compute_word_count_excludes_code_blocks():
    text = "正文两个字\n```python\nsome code that should not count towards word count\n```"
    assert enrich.compute_word_count(text) == 5


def test_compute_word_count_excludes_markdown_link_urls():
    # 链接锚文本（"详情"）算正文，但 URL 本身不该被计入字数——不剥离的话仅这一个
    # URL 就能贡献 100+ 字，正确剥离后应该远小于这个量级（此前未剥离，会注水，I2）
    long_url = "https://example.com/" + "x" * 100
    text = f"正文两个字 [详情]({long_url}) 结尾两个字"
    assert enrich.compute_word_count(text) < 20


def test_compute_word_count_excludes_local_image_refs():
    long_hash = "abcdef0123456789" * 3
    text = f"正文两个字![配图]({enrich.IMAGE_URL_PREFIX}2026-07-05/{long_hash}.jpg)结尾两个字"
    assert enrich.compute_word_count(text) < 20


# ---------------------------------------------------------------------------
# _arxiv_fulltext_url：arxiv 摘要页 URL 改写成全文 HTML 端点（只有全部 83 篇真实
# 生产 arxiv 原文只抓到摘要这个问题的根因，见 .claude/memory/known_issues.md）。
# ---------------------------------------------------------------------------

def test_arxiv_fulltext_url_rewrites_abs_to_html():
    assert enrich._arxiv_fulltext_url("http://arxiv.org/abs/2607.02140v1") == "https://arxiv.org/html/2607.02140v1"
    assert enrich._arxiv_fulltext_url("https://arxiv.org/abs/2607.02140v1") == "https://arxiv.org/html/2607.02140v1"


def test_arxiv_fulltext_url_none_for_non_arxiv_source():
    assert enrich._arxiv_fulltext_url("https://openai.com/index/introducing-genebench-pro") is None


def test_arxiv_fulltext_url_none_for_already_html_or_pdf_path():
    assert enrich._arxiv_fulltext_url("https://arxiv.org/html/2607.02140v1") is None
    assert enrich._arxiv_fulltext_url("https://arxiv.org/pdf/2607.02140v1") is None


def test_arxiv_fulltext_pending_non_arxiv_always_none():
    assert enrich._arxiv_fulltext_pending(False) is None
    assert enrich._arxiv_fulltext_pending(False, got_arxiv_fulltext=True) is None


def test_arxiv_fulltext_pending_arxiv_reflects_got_fulltext():
    assert enrich._arxiv_fulltext_pending(True, got_arxiv_fulltext=True) is False
    assert enrich._arxiv_fulltext_pending(True, got_arxiv_fulltext=False) is True
    assert enrich._arxiv_fulltext_pending(True) is True  # 默认 got_arxiv_fulltext=False（SSRF 拦截/全部通道失败场景）


def test_fetch_channels_all_return_uniform_two_tuple_shape(mocker):
    """2026-07-08 修复：三个抓取通道必须统一返回 (markdown, got_arxiv_fulltext) 二元组，
    调用方不能再靠 `if channel == "direct"` 按名字特判拆包（此前 jina/playwright 仍
    返回裸字符串）。_fetch_direct 本身已有 test_arxiv_abs_* 系列单独覆盖，这里只验证
    jina/playwright 两个包装函数的返回形状，以及 _FETCH_CHANNELS 里登记的确实是这两个
    包装函数而不是原始的 _fetch_via_jina/_fetch_via_playwright。"""
    mocker.patch.object(enrich, "_fetch_via_jina", return_value="jina markdown")
    mocker.patch.object(enrich, "_fetch_via_playwright", return_value="playwright markdown")

    assert enrich._fetch_via_jina_channel("https://example.com/a") == ("jina markdown", False)
    assert enrich._fetch_via_playwright_channel("https://example.com/a") == ("playwright markdown", False)

    channel_fns = {name: fn for fn, name in enrich._FETCH_CHANNELS}
    assert channel_fns["jina"] is enrich._fetch_via_jina_channel
    assert channel_fns["playwright"] is enrich._fetch_via_playwright_channel


# ---------------------------------------------------------------------------
# arxiv 抓取内容自带标题/摘要页头尾噪声清洗——真实批次实测发现全文/摘要页正文
# 都会自带一份论文标题，跟 documents.title 重复导致"标题渲染两遍"；摘要页额外带
# 缩进侧边栏噪声，缩进会被 markdown 解释成代码块（见 .claude/memory/known_issues.md）。
# ---------------------------------------------------------------------------

def test_leading_h1_stripped_from_fetched_markdown():
    # 2026-07-07：这条清洗此前只对 arxiv 生效，现在挪进三通道共用的
    # _clean_fetched_markdown，非 arxiv 来源（如普通新闻站点）同样要去掉正文
    # 开头跟 documents.title 重复的 H1（见 .claude/memory/known_issues.md）。
    raw = "# Some Paper Title\n\n###### Abstract\n\nThe actual abstract text starts here."
    cleaned = enrich._strip_leading_title_h1(raw)
    assert cleaned == "###### Abstract\n\nThe actual abstract text starts here."


def test_clean_arxiv_abs_markdown_strips_header_and_sidebar():
    raw = (
        "# Computer Science > Machine Learning\n\n"
        "  [Submitted on 2 Jul 2026]\n\n"
        "    # Title:Probing Chemical Language Models\n\n"
        "View PDFAbstract:This is the real abstract content.\n\n"
        "      \n      Full-text links:\n      \n          "
        "![license icon](ainews-media://x/y.png) view license\n\n          view license"
    )
    cleaned = enrich._clean_arxiv_abs_markdown(raw)
    assert cleaned == "This is the real abstract content."


def test_check_arxiv_fulltext_activity_is_actually_lightweight(mocker):
    """2026-07-08 修复：轻量可用性探测不应该调用 _process_images（配图下载/落盘），
    否则和真正的 _try_arxiv_fulltext 完整抓取重复了一遍最贵的那部分开销。"""
    fake_response = mocker.Mock(status_code=200, text="<html>full paper body</html>")
    mocker.patch.object(enrich, "_safe_get", return_value=fake_response)
    mocker.patch.object(enrich.trafilatura, "extract", return_value="# Title\n\nfull paper body text")
    process_images_mock = mocker.patch.object(enrich, "_process_images")

    assert enrich.check_arxiv_fulltext_activity("http://arxiv.org/abs/2607.02423v1") is True
    process_images_mock.assert_not_called()


def test_check_arxiv_fulltext_activity_false_when_not_yet_rendered(mocker):
    fake_response = mocker.Mock(status_code=404, text="")
    mocker.patch.object(enrich, "_safe_get", return_value=fake_response)

    assert enrich.check_arxiv_fulltext_activity("http://arxiv.org/abs/2607.02423v1") is False


def test_check_arxiv_fulltext_activity_false_for_non_arxiv_url():
    assert enrich.check_arxiv_fulltext_activity("https://openai.com/index/some-post") is False


def test_arxiv_abs_cleaning_must_run_before_generic_pipeline():
    # 2026-07-08 回归：通用管线 _clean_fetched_markdown 的 _strip_leading_title_h1 会把
    # 开头 "# Computer Science > Machine Learning" 这个 H1 剥掉，而 _clean_arxiv_abs_markdown
    # 的 _ARXIV_ABS_HEADER_RE 要求字符串仍以 "^#" 开头才能整体匹配掉
    # [Submitted...]/Title:/View PDFAbstract: 这段样板文字——顺序反了会导致这些文字
    # 原样残留进正文（真实复现过）。_fetch_direct 必须先调用 _clean_arxiv_abs_markdown
    # 再调用 _clean_fetched_markdown，这里直接测这个组合顺序，防止再犯。
    raw = (
        "# Computer Science > Machine Learning\n\n"
        "  [Submitted on 2 Jul 2026]\n\n"
        "    # Title:Probing Chemical Language Models\n\n"
        "View PDFAbstract:This is the real abstract content.\n\n"
        "      \n      Full-text links:\n      \n          "
        "![license icon](ainews-media://x/y.png) view license\n\n          view license"
    )
    correct_order = enrich._clean_fetched_markdown(enrich._clean_arxiv_abs_markdown(raw))
    assert correct_order == "This is the real abstract content."

    wrong_order = enrich._clean_arxiv_abs_markdown(enrich._clean_fetched_markdown(raw))
    assert wrong_order != "This is the real abstract content."
    assert "[Submitted" in wrong_order or "View PDF" in wrong_order


# ---------------------------------------------------------------------------
# _chunk_paragraphs：单个段落超过 max_chars 时必须硬切，否则全文版 arxiv 论文里
# 没有空行分隔的超长参考文献列表/附录会整段塞进一个分块，翻译输出被 max_tokens
# 截断触发 IncompleteOutputException（真实批次实测崩溃过，见 known_issues.md）。
# ---------------------------------------------------------------------------

def test_chunk_paragraphs_splits_oversized_single_paragraph():
    huge_paragraph = "x" * 5000  # 单段就超过 max_chars=100，且没有任何空行可切
    chunks = enrich._chunk_paragraphs(huge_paragraph, max_chars=100)
    assert all(len(c) <= 100 for c in chunks)
    assert "".join(chunks) == huge_paragraph  # 硬切不能丢字符


def test_chunk_paragraphs_normal_case_unaffected():
    text = "第一段。\n\n第二段。\n\n第三段。"
    chunks = enrich._chunk_paragraphs(text, max_chars=100)
    assert chunks == [text]  # 都在上限内，仍然合并成一块，跟硬切逻辑加入前行为一致


# ---------------------------------------------------------------------------
# _translate_chunk_with_retry：主模型调用异常（截断/JSON 格式错误）时换
# qwen3.6-flash 兜底重试一次，不再直接放弃保留原文（2026-07-08 新增）。
# ---------------------------------------------------------------------------

def test_translate_chunk_retries_with_fallback_model_on_incomplete_output(mocker):
    from instructor.core.exceptions import IncompleteOutputException

    calls = []

    def fake_call_structured(*, model, max_tokens, **kwargs):
        calls.append((model, max_tokens))
        if model == enrich.TRANSLATE_MODEL:
            raise IncompleteOutputException()
        return enrich.ChunkTranslation(translated_text="这是一段完整的中文译文，用于满足 CJK 占比校验。")

    mocker.patch.object(enrich, "call_structured", side_effect=fake_call_structured)
    translated, skipped, retry_count, had_residual_failure = enrich._translate_chunk_with_retry(
        "Some English paragraph to translate."
    )

    assert translated == "这是一段完整的中文译文，用于满足 CJK 占比校验。"
    assert skipped is False
    assert had_residual_failure is False
    # 第一次用主模型（失败），第二次换成 fallback 模型且 max_tokens 更大
    assert calls[0][0] == enrich.TRANSLATE_MODEL
    assert calls[1] == (enrich._TRANSLATE_FALLBACK_MODEL, enrich._TRANSLATE_FALLBACK_MAX_TOKENS)


def test_translate_chunk_keeps_original_when_too_small_to_split_further(mocker):
    """主模型 + 换模型都失败，且分块本身已经低于安全切分下限——第三层兜底直接放弃
    细分，返回原文并如实标记 had_residual_failure=True（不再向上抛异常）。"""
    from instructor.core.exceptions import IncompleteOutputException

    mocker.patch.object(enrich, "call_structured", side_effect=IncompleteOutputException())
    short_chunk = "Some English paragraph to translate."
    assert len(short_chunk) <= enrich._MIN_SPLIT_CHARS

    translated, skipped, retry_count, had_residual_failure = enrich._translate_chunk_with_retry(short_chunk)

    assert translated == short_chunk
    assert had_residual_failure is True


# ---------------------------------------------------------------------------
# _split_chunk_at_safe_boundary / _translate_oversized_chunk：第三层兜底的安全
# 切分逻辑，不能切进表格/代码块内部（2026-07-08 新增）。
# ---------------------------------------------------------------------------

def test_split_chunk_avoids_cutting_inside_table():
    header = "| Col A | Col B |\n| --- | --- |\n"
    rows = "".join(f"| val{i} | data{i} |\n" for i in range(80))  # 单张大表撑满整个分块
    intro = "这是表格前的说明文字。\n\n"
    chunk = intro + header + rows

    halves = enrich._split_chunk_at_safe_boundary(chunk)

    assert halves is not None
    for half in halves:
        lines = [line for line in half.split("\n") if line.strip()]
        table_lines = [line for line in lines if enrich._TABLE_ROW_RE.match(line)]
        if table_lines:
            # 切出来的这一半里，凡是表格行必须是连续的一整段，不能出现"表格行→非表格行→
            # 表格行"这种夹心结构（说明表格被切碎后又跟别的内容混在一起）。
            first_table_idx = lines.index(table_lines[0])
            last_table_idx = lines.index(table_lines[-1])
            assert lines[first_table_idx : last_table_idx + 1] == table_lines


def test_split_chunk_returns_none_when_entire_chunk_is_one_atomic_block():
    # 从头到尾都是同一张表，没有任何非表格内容可以作为切分边界
    chunk = "| Col A | Col B |\n| --- | --- |\n" + "".join(f"| val{i} | data{i} |\n" for i in range(50))

    assert enrich._split_chunk_at_safe_boundary(chunk) is None


def test_cjk_retry_loop_reuses_fallback_model_not_main_model(mocker):
    """2026-07-08 回归修复：策略链最终生效的是二层换模型兜底时，CJK 复检重试必须
    继续用这个已经证明有效的 fallback 模型，不能落回刚刚失败过的主模型（此前的
    实现无条件用主模型重试，真实跑出过把刚成功的译文整体丢弃的情况）。"""
    from instructor.core.exceptions import IncompleteOutputException

    calls = []

    def fake_call_structured(*, model, max_tokens, **kwargs):
        calls.append(model)
        if model == enrich.TRANSLATE_MODEL:
            raise IncompleteOutputException()
        if calls.count(enrich._TRANSLATE_FALLBACK_MODEL) == 1:
            return enrich.ChunkTranslation(translated_text="low cjk ratio output text here 低")
        return enrich.ChunkTranslation(translated_text="这是一段完整的中文译文，满足复检占比要求。")

    mocker.patch.object(enrich, "call_structured", side_effect=fake_call_structured)
    translated, skipped, retry_count, had_residual_failure = enrich._translate_chunk_with_retry(
        "Some English paragraph to translate."
    )

    assert had_residual_failure is False
    assert retry_count == 1
    assert translated == "这是一段完整的中文译文，满足复检占比要求。"
    # 第一次主模型失败，之后两次都必须是 fallback 模型，不能有任何一次落回主模型
    assert calls[1:] == [enrich._TRANSLATE_FALLBACK_MODEL, enrich._TRANSLATE_FALLBACK_MODEL]


def test_cjk_retry_loop_survives_exception_without_losing_prior_translation(mocker):
    """CJK 复检重试本身如果再次调用异常，不应该无保护地冒泡把已经拿到的译文丢弃，
    应该保留复检前的结果、跳出循环。"""
    from instructor.core.exceptions import IncompleteOutputException

    calls = []

    def fake_call_structured(*, model, max_tokens, **kwargs):
        calls.append(model)
        if model == enrich.TRANSLATE_MODEL and len(calls) == 1:
            raise IncompleteOutputException()
        if calls.count(enrich._TRANSLATE_FALLBACK_MODEL) == 1:
            return enrich.ChunkTranslation(translated_text="low cjk ratio 低")
        raise IncompleteOutputException()

    mocker.patch.object(enrich, "call_structured", side_effect=fake_call_structured)
    translated, skipped, retry_count, had_residual_failure = enrich._translate_chunk_with_retry(
        "Some English paragraph to translate."
    )

    # 复检重试异常被吞掉，保留复检前（CJK 占比虽然低但确实拿到）的译文，不抛异常、
    # 不整体丢弃。
    assert translated == "low cjk ratio 低"
    assert had_residual_failure is False


def test_translate_oversized_chunk_uses_fallback_model_for_halves(mocker):
    """安全切分兜底翻译每一半时，直接用已证明更可靠的 fallback 模型，不退回刚刚
    对整块失败过的主模型（2026-07-08 修复）。"""
    from instructor.core.exceptions import IncompleteOutputException

    para_a = "第一段英文内容。" * 60
    para_b = "第二段英文内容。" * 60
    chunk = para_a + "\n\n" + para_b
    assert len(chunk) > enrich._MIN_SPLIT_CHARS

    calls = []

    def fake_translate_chunk(text, **kwargs):
        calls.append(kwargs.get("model"))
        return f"翻译：{text[:10]}"

    mocker.patch.object(enrich, "_translate_chunk", side_effect=fake_translate_chunk)
    translated, had_residual_failure = enrich._translate_oversized_chunk(chunk, IncompleteOutputException())

    assert had_residual_failure is False
    assert calls == [enrich._TRANSLATE_FALLBACK_MODEL, enrich._TRANSLATE_FALLBACK_MODEL]


def test_translate_oversized_chunk_splits_and_merges_successfully(mocker):
    from instructor.core.exceptions import IncompleteOutputException

    para_a = "第一段英文内容。" * 60
    para_b = "第二段英文内容。" * 60
    chunk = para_a + "\n\n" + para_b
    assert len(chunk) > enrich._MIN_SPLIT_CHARS

    def fake_translate_chunk(text, **kwargs):
        return f"翻译：{text[:10]}"

    mocker.patch.object(enrich, "_translate_chunk", side_effect=fake_translate_chunk)
    translated, had_residual_failure = enrich._translate_oversized_chunk(chunk, IncompleteOutputException())

    assert had_residual_failure is False
    assert translated.count("翻译：") == 2  # 两半各自独立翻译成功，拼接后能看到两次结果
