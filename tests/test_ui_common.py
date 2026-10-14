"""Amount formatting: a real amount never prints as zero."""
import pytest

from ui_common import fmt_lapse, fmt_lapse_dp


@pytest.mark.parametrize("ticks, shown", [
    (0, "0.0000 LAPSE"),
    (1, "1 tick"),
    (37, "37 ticks"),
    (4_999, "4,999 ticks"),
    (5_000, "0.0001 LAPSE"),
    (12_345, "0.0001 LAPSE"),
    (100_000_000, "1.0000 LAPSE"),
    (2_870_825_987_856, "28,708.2599 LAPSE"),
])
def test_fmt_lapse_dp_uses_ticks_when_decimals_would_read_zero(ticks, shown):
    assert fmt_lapse_dp(ticks) == shown


@pytest.mark.parametrize("ticks, shown", [
    (0, "0 LAPSE"),
    (37, "37 ticks"),
    (50_000_000, "0.5000 LAPSE"),
    (100_000_000, "1 LAPSE"),
    (2_870_825_987_856, "28,708 LAPSE"),
])
def test_fmt_lapse_never_reads_zero_for_a_real_amount(ticks, shown):
    assert fmt_lapse(ticks) == shown
