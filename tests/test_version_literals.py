"""Version numbers written as a word and digits (closure ticket 09, red team D1).

The literal check used to skip any word directly followed by digits
(``_WORD_THEN_DIGITS``, commit 34d113f), because the engine glued words to
the digits after them (``Section3.2``). Closure ticket 02 fixed the word
spaces, so a changed version (``Wan2.1`` written as ``Wan2.2``) no longer
passes. These call the checks directly on a source paragraph and its
translation, as the review does.
"""

import pytest

from zotero_translate.checks import problems

SOURCES = {
    "Wan2.1": "We fine-tune Wan2.1 on our videos for 3 epochs.",
    "Qwen2.5-VL": "The planner is Qwen2.5-VL with a frozen vision encoder.",
    "Veo3": "Videos from Veo3 are rated higher by the judges.",
}


@pytest.mark.parametrize(
    "version, translation",
    [
        ("Wan2.1", "我们在自己的视频上对 Wan2.2 微调 3 个轮次。"),
        ("Qwen2.5-VL", "规划器是 Qwen2-VL，视觉编码器保持冻结。"),
        ("Veo3", "评审认为 Veo2 生成的视频更好。"),
    ],
)
def test_a_changed_version_fails_the_check(version, translation):
    found = problems(SOURCES[version], translation)

    assert f"字面值 {version} 被改动或缺失" in found


@pytest.mark.parametrize(
    "version, translation",
    [
        ("Wan2.1", "我们在自己的视频上对 Wan 2.1 微调 3 个轮次。"),
        ("Qwen2.5-VL", "规划器是 Qwen2.5-VL，视觉编码器保持冻结。"),
        ("Veo3", "评审认为 Veo3 生成的视频更好。"),
    ],
)
def test_a_kept_version_passes_also_with_a_space(version, translation):
    assert problems(SOURCES[version], translation) == []
