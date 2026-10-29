"""最近秩分位数保持原有边界与空值口径。"""
import subprocess
import sys

import pytest


@pytest.mark.parametrize("values,q,expected", [
    ([], .95, None), ([7], .5, 7), ([4, 1, 3, 2], .5, 2),
    ([4, 1, 3, 2], .95, 4), ([1, 1, 1], .95, 1),
    (list(range(1, 21)), .95, 19),
])
def test_nearest_rank_preserves_result_and_input(values, q, expected):
    from app.shared.utils.statistics import percentile
    original = list(values)
    assert percentile(values, q) == expected
    assert values == original


def test_percentile_import_remains_offline_and_config_free():
    code = (
        "import sys; from app.shared.utils.statistics import percentile; "
        "assert percentile([], .95) is None; "
        "assert not any(n.startswith(('app.rag', 'app.shared.config')) for n in sys.modules)"
    )
    assert subprocess.run([sys.executable, "-c", code], capture_output=True).returncode == 0
