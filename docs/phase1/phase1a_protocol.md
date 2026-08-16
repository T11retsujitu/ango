# Phase 1A — Cost-Aware Abstention 実験プロトコル v1(凍結)

- 凍結日: 2026-08-16(**評価実行前に本ファイルを確定。この版では定義・閾値を変更しない。**
  変更したくなったら v2 として新たに凍結してから行う — findings 恒久ルール2に従う)
- 実装: `src/mce/research/abstention.py`(PROTOCOL = "phase1a_v1")
- 目的: alpha discovery ではなく **Judge の検証**(ROADMAP Phase 1)。
  「予測エッジが取引コストを超えるときだけ取引する」という abstention 規則が、
  turnover と net performance を改善するかを機械判定する。

## 1. 参照研究と replication class

> Machine Learning-Based Bitcoin Trading Under Transaction Costs: Evidence From
> Walk-Forward Forecasting (arXiv:2606.00060)

- 原論文: BTC-USDT **1時間足**・27-fold walk-forward。
- 本実験: OKX BTC-USDT-SWAP **5分足**・research 区間のみ。
- したがって **`replication_class = method_transfer`**。exact replication ではない。
  採用するのは「forecast → trade conversion(エッジ < コストなら取引しない)」の
  考え方のみで、原論文の性能数値は一切参照値としない。

## 2. データと分割(凍結)

| 項目 | 値 |
|---|---|
| 対象 | features(observable のみ、loader guard 経由)+ labels の `fwd_open_return_1h` |
| 期間 | research split のみ(2023-11-19 〜 2025-07-01)。**validation は温存**(本プロトコルの結論を凍結後、必要なら validation で一度だけ追試する) |
| folds | walk-forward: train 120日 / test 30日 / step 30日(約15 fold) |
| embargo | train 窓の末尾 70分(= horizon 12 bars + 2 bars)を学習から除外。ラベルが test 窓を覗かない |

## 3. ラベルとモデル(凍結)

- ラベル: `fwd_open_return_1h` = open[t+1+12] / open[t+1] − 1(**執行整合**:
  signal at close[t] → entry open[t+1] → exit open[t+13]。ts 一致 join、欠損なら null)
- 学習目標: sign(label) の2値分類
- モデル: ロジスティック回帰(numpy IRLS、ridge l2=1e-3、決定的。sklearn/XGBoost 不使用)
- 入力(5列固定): `return_5m, return_1h, volume_ratio_20, drift_20d, realized_vol_20d`
  - 標準化は train 窓の平均・標準偏差のみ使用
  - feature が1つでも null の行は予測せず **abstain(flat)**
- エッジ換算: `edge_bps = (2·P(up) − 1) × mean(|label|)_train × 10⁴`
  (方向確率 × train 窓の平均絶対リターン。分類確率をエッジへ変換する最小の規則)

## 4. Arms(凍結)

各 fold の test 窓を、同一の Phase 0 Judge(next-bar 執行・コスト・metrics)で評価する。

| arm | target 規則 |
|---|---|
| `model_sign` | sign(edge)。**abstention しない対照**(feature 欠損時のみ flat) |
| `abstention` | \|edge\| > **往復コスト**(シナリオの roundtrip_bps)のときのみ sign(edge) |
| `random_abstention` | model_sign の連続シグナル区間を、abstention と同じ exposure 比率になる確率で無作為に残す(seed = 20260816 + fold番号)。**「単に取引を減らしただけ」との識別用対照** |
| `buy_and_hold` | 文脈参照用 |

コストシナリオは `maker_low`(往復2bps)と `base_taker`(往復10bps)の両方を実行し、
別々に判定・報告する(選別報告禁止: 両方とも報告する)。

## 5. 記録

fold ごとに全 arm の net/gross return, turnover, trade 数, Sharpe, exposure,
hit rate, break-even cost を JSON(`experiments/phase1a/phase1a_<cost>.json`、
追記専用)へ保存。features/labels の sha256 と source commit を含める。

## 6. 判定基準(凍結。結果を見てからの変更禁止)

有効 fold(train 100行以上・test 非空)に対して:

- **J1(turnover 削減)**: abstention の turnover < model_sign の turnover が fold の 80% 以上
- **J2(net 改善)**: abstention の net > model_sign の net が fold の 2/3 以上、かつ差の中央値 > 0
- **J3(random 対照超え)**: abstention の net > random_abstention の net が fold の 2/3 以上
- **ガード**: abstention のクローズ済み trade 合計 < 30 なら **判定不能(insufficient_trades)**

判定:

- J1 ∧ J2 ∧ J3 → **abstention_supported**(abstention 規則に価値がある)
- それ以外 → **abstention_rejected**(ROADMAP Phase 1A の Failure 条項に従い棄却)

注意: J2 は「モデルにエッジがなくても取引を減らせば損失が減る」だけで成立しうる。
**J3(exposure を揃えた無作為対照との比較)が本質の検定**であり、J3 が落ちれば
「abstention の価値はエッジ選別ではなく単なる取引削減」と結論する。

abstention の絶対収益(net > 0 の fold 比率)は判定に使わず参考記録とする
(事前予想では taker で負、maker でゼロ近傍。本実験の主眼は Judge の検証であり
収益性ではない)。

## 7. 事前予想(2026-08-16 記録。findings 恒久ルール3)

1. J1 はほぼ機械的に成立(~80% 確信)
2. J2 も成立しやすい(取引削減効果込み)
3. **J3 が本丸で、成立確率は五分五分以下**。LogReg + OHLCV 5列の 5分足方向予測に
   選別力がある可能性は低い(方向精度 50.5〜52% 想定)
4. 絶対収益: base_taker で全 fold 負、maker_low でゼロ近傍(−5〜+5bps/trade)
5. 総合 verdict 予想: base_taker で `abstention_rejected` または
   `insufficient_trades`(閾値10bpsで取引がほぼ消える)、maker_low は判定五分五分

## 8. 実行手順

```sh
uv run python -m mce.labels        # fwd_open_return_1h を含む labels を再生成
uv run python -m mce.manifest      # manifest 更新
uv run python -m mce.research.abstention --cost maker_low
uv run python -m mce.research.abstention --cost base_taker
git add experiments/phase1a data/manifests && git commit
```

結果の解釈・結論は `docs/findings/` に日付付きで台帳化する(本ファイルは変更しない)。
