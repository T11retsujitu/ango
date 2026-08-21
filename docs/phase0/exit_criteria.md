# Phase 0 Exit Criteria — 自動テスト対応表

ROADMAP「Phase 0 — Deterministic Judge / Exit Criteria」の各条件と、
それを機械的に検証するテストの対応。`uv run pytest` が green であることが
Phase 0 完了の判定基準。

| # | Exit criterion (ROADMAP) | 検証テスト |
|---|---|---|
| 1 | future bar を書き換えるテストで過去 signal が変わらない | `tests/test_labels.py::test_future_mutation_does_not_change_past_observables`(observable 全列の不変性。signal は observable の純関数なので同時に保証される) |
| 2 | cost=0 と cost>0 で期待通り PnL が変化 | `tests/test_costs.py::test_zero_cost_net_equals_gross`, `test_cost_reduces_net_by_turnover_times_rate`, `test_cost_monotonicity` |
| 3 | execution を 1 bar 遅らせると結果が変化 | `tests/test_execution.py::test_execution_delay_changes_result`(close 執行でなく open[t+1] 執行であることは `test_fill_is_next_bar_open_not_close`) |
| 4 | baseline strategies が再現可能 | `tests/test_engine.py::test_always_flat_and_buy_and_hold`, `test_naive_momentum_uses_observable_only`(値の手計算一致) |
| 5 | random seed 固定で同一結果 | `tests/test_engine.py::test_determinism_same_seed_same_result`, `test_artifact_determinism_modulo_metadata` |
| 6 | unit test が通る | `uv run pytest`(全テスト) |

## 追加で強制している契約(ROADMAP 本文由来)

| 契約 | 検証テスト |
|---|---|
| forward return(fwd_*)は strategy feature として利用不可 | `tests/test_labels.py::test_features_contain_no_labels_and_all_availability_declared`, `tests/test_data_loader.py::test_fwd_column_raises_leakage_error` |
| 欠損バーを跨いで誤った 5m/1h リターンを計算しない | `tests/test_features.py::test_returns_and_gap_safety`, `tests/test_execution.py::test_missing_bar_*` |
| final_oos は通常 API から封印 | `tests/test_data_loader.py::test_final_oos_is_sealed` |
| split 境界の凍結定義 | `tests/test_splits.py::test_boundary_assignment` |
| walk-forward folds が final_oos に食い込まない | `tests/test_splits.py::test_walk_forward_folds_reject_final_oos` |
| 実験 artifact(config・manifest hash・commit・seed・replication_class) | `tests/test_engine.py::test_artifact_roundtrip` |

## 実データでの確認手順(ローカル PC)

```sh
uv run python -m mce.features     # observable 再生成(fwd_* が消える)
uv run python -m mce.labels       # data/labels/ へラベル分離出力
uv run python -m mce.manifest --all   # data/manifests/*.json 生成(git 管理。対象の明示が必須)
uv run python -m mce.backtest --strategy buy_and_hold --split research --cost base_taker
uv run python -m mce.backtest --strategy random --seed 42 --split research --cost base_taker
```

2 回目の同一コマンド実行(random, 同 seed)で metrics が完全一致すること。
