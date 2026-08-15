"""_fetch_backward の停止判定(差分取得 / バックフィル)のテスト。

偽のページ関数で OKX のページング(after より古いものを新しい順に返す)を再現する。
"""

import pytest

from mce import config
from mce.ingest import _fetch_backward

MIN_MS = 60_000
# 全履歴: 0分〜99分の1分刻み100本
ALL_TS = [m * MIN_MS for m in range(100)]
PAGE_SIZE = 10


@pytest.fixture(autouse=True)
def raw_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "RAW_DIR", tmp_path)


def fake_page(after):
    ts_desc = sorted((t for t in ALL_TS if after is None or t < after), reverse=True)
    rows = [[str(t)] for t in ts_desc[:PAGE_SIZE]]
    return {"data": rows}, {"after": after}


def collected_ts(rows):
    return sorted(int(r[0]) for r in rows)


def test_no_existing_data_fetches_to_start():
    rows = _fetch_backward(fake_page, "test", start_ms=50 * MIN_MS, existing_range=(None, None))
    assert min(collected_ts(rows)) <= 50 * MIN_MS


def test_incremental_stops_at_existing_max():
    # 既存: 0〜79分。開始指定(50分)は既存範囲内 → 既存最終 ts で停止(1〜2ページで済む)
    rows = _fetch_backward(
        fake_page, "test", start_ms=50 * MIN_MS, existing_range=(0, 79 * MIN_MS)
    )
    assert min(collected_ts(rows)) >= 70 * MIN_MS  # 79分に達した最初のページで停止


def test_backfill_goes_past_existing_data():
    # 既存: 60〜99分。開始指定(10分)は既存より古い → 10分まで遡る
    rows = _fetch_backward(
        fake_page, "test", start_ms=10 * MIN_MS, existing_range=(60 * MIN_MS, 99 * MIN_MS)
    )
    assert min(collected_ts(rows)) <= 10 * MIN_MS
