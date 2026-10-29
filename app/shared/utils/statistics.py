"""不加载配置的统计计算。调用方负责选择有效样本。"""
import math


def percentile(values, q):
    """返回最近秩分位数；空样本保持 None，不填充零值。"""
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, math.ceil(len(ordered) * q) - 1)]
