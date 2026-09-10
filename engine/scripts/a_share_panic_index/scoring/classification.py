"""固定等级，不使用动态历史边界。"""


def classify_level(score: float) -> str:
    value = max(0.0, min(100.0, float(score)))
    if value < 25:
        return "极度平静"
    if value < 40:
        return "偏平静"
    if value < 60:
        return "中性"
    if value < 75:
        return "偏恐慌"
    return "极度恐慌"
