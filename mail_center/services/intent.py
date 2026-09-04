"""模块五的评分引擎：根据回复内容判定意向等级。

规则引擎（关键词匹配），后续可替换为 LLM 语义分析。
"""

from .. import config

_LEVEL_ORDER = {"none": 0, "low": 1, "medium": 2, "high": 3}


def score_reply(text: str) -> tuple[int, str]:
    """返回 (分数, 等级)。

    计分规则：
      高意向关键词 +30/个（封顶 90）
      中意向关键词 +10/个
      低意向关键词 -40/个，命中即至少降为 low
      带问号 +5（对方在提问）
      提到具体日期词 +10
    """
    low_text = text.lower()

    score = 0
    high_hits = sum(1 for kw in config.INTENT_HIGH_KEYWORDS if kw.lower() in low_text)
    mid_hits = sum(1 for kw in config.INTENT_MID_KEYWORDS if kw.lower() in low_text)
    low_hits = sum(1 for kw in config.INTENT_LOW_KEYWORDS if kw.lower() in low_text)

    score += min(high_hits * 30, 90)
    score += mid_hits * 10
    score -= low_hits * 40
    if "？" in text or "?" in text:
        score += 5

    if low_hits > 0:
        level = "low" if score > -20 else "none"
    elif high_hits > 0:
        level = "high" if score >= 60 else "medium"
    elif mid_hits > 0:
        level = "medium" if score >= 20 else "low"
    else:
        # 无关键词命中：有回复本身是弱信号
        level = "low"
        score = max(score, 5)

    return score, level
