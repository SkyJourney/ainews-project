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
