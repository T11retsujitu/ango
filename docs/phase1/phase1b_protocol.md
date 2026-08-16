# Phase 1B — Clock Phase / Quarter-Hour スクリーニング プロトコル v1(凍結)

- 凍結日: 2026-08-16(**評価実行前に確定。この版では定義・閾値を変更しない。**
  変更は v2 として再凍結してから — findings 恒久ルール2)
- 実装: `src/mce/research/clock_phase.py`(PROTOCOL = "phase1b_v1")
- 目的: alpha discovery ではなく **Judge/データ基盤の検証**と、ROADMAP Phase 7 の
  分岐判断(「5分足で構造が見える場合のみ aggTrades へ進む」)の入力を作ること。

## 1. 参照研究と replication class

> The Quarter-Hour Effect: Periodic Algorithmic Trading and Return Predictability
> in Cryptocurrency Futures (arXiv:2607.09426)

- 原論文: **Binance** perpetual、1分足を含む高解像度。
- 本実験: **OKX** BTC-USDT-SWAP 5分足のみ。
- したがって **`replication_class = cross_exchange_validation`**(replication-inspired)。
  exact replication ではない。5分足では 1 分単位の境界 placebo は表現できないため、
  表現可能な phase 単位(5分)に合わせる(ROADMAP Phase 1B の指示どおり)。

## 2. データ(凍結)

- features(observable、loader guard 経由)+ labels(`fwd_return_5m`,
  `fwd_open_return_1h`)の明示 join。
- 期間: research split のみ(2023-11-19 〜 2025-07-01)。validation は温存。
- 対象 phase family:
  - `minute_mod_15` ∈ {0, 5, 10} — quarter-hour。**真の境界 = 0**
  - `minute_mod_60` ∈ {0, 5, …, 55} — hour。**真の境界 = 0**
  - `hour_utc`, `weekday_utc` — 記述統計のみ(判定なし。H1/H2 は台帳で確定済み)

## 3. 統計量(凍結)

phase 群 p ごとに:

- **directional**: mean fwd_return_5m(bps)。「境界バーの close で signal →
  次バー」という執行可能な向きの予測性
- **activity**: mean |return_5m|(bps)と mean volume。周期的な活動構造
- **expectancy 参考**: mean fwd_open_return_1h(bps)
- 境界候補 p の効果量 = |mean(p) − mean(rest)|、t は Welch の2標本 t

## 4. Placebo / 帰無分布(凍結)

1. **shifted phase**: 全 phase を境界候補として同じ統計を計算し選別なしで報告する
   (m15 は 0/5/10 の3候補、m60 は 12候補)。真の境界 0 のみが通り、
   シフト候補が通らないことを「clock-anchored」の条件とする。
2. **random phase(permutation)**: fwd_return_5m を n_permutations=500 回シャッフルし、
   各回の **max-statistic**(全 phase 候補の効果量の最大値)で帰無分布を作る。
   p 値 = (1 + #{null_max ≥ 実測効果量}) / (501)。max 統計により多重比較
   (family-wise)を保守的に制御する。seed = 20260817 固定。

## 5. 判定基準(凍結。結果を見てからの変更禁止)

phase 候補 p が **directional 構造**を持つ条件(D1、全て必要):

- Welch |t| ≥ 3
- permutation p(FWER)< 0.01
- 効果量 ≥ 1bps
- research 前半/後半で効果の符号が一致

family の verdict(argmax 方式):

vs-rest 対比には補集合効果がある(ある phase が持ち上がると他 phase の「対 rest」差も
自動的に生じ、特に候補3つの m15 では全候補が同時に有意になりうる)。そのため
「他候補が通らないこと」ではなく **効果量最大の phase の位置** で判定する:

- 効果量最大の phase が真の境界(0)であり、かつ D1 を通過 → **clock_anchored_directional**
- 効果量最大の phase が境界以外で D1 を通過 → **directional_at_shifted_phase**
  (placebo 警報。clock 仮説として不合格)
- 効果量最大の phase が D1 を通過しない → **no_directional_structure**

(この argmax 方式は合成データでの検証中(実データ実行前)に、当初の
「他候補不通過」方式が補集合効果で機能しないことが判明したため置換した。
実データ実行後の変更ではない。)

activity は判定でなく確認: 境界候補の |return_5m| Welch |t| ≥ 5 を activity 構造
「確認」とし記録(方向性なし・取引価値の主張なし)。

**ROADMAP 分岐**: いずれかの family が clock_anchored_directional →
aggTrades 検討へ進む根拠となる。それ以外 → activity-only を記録し、
clock 単体の方向性 alpha 仮説は保留。

経済的注記: D1 を通過した効果量も maker 往復2bps / taker 往復10bps と必ず併記する
(コスト未満は「統計的事実でありエッジではない」— 恒久ルール5)。

## 6. 補助集計(判定なし・記述のみ)

- `minute_mod_15` × volatility regime(realized_vol_20d の research 窓中央値で
  high/low 2分。スクリーニング目的の in-sample 記述であり walk-forward ではない
  ことを明記)ごとの directional / activity
- `hour_utc`, `weekday_utc` ごとの同統計

## 7. 事前予想(2026-08-16 記録)

1. **activity 構造は m15・m60 とも確認される**(~90% 確信。原論文の境界活動と
   H2/H4 の存在から)
2. **directional は clock_anchored まで届かない**(~75-80% 確信で
   no_directional_structure)。届いた場合も効果量は 1〜3bps でコスト未満
3. hour_utc の記述統計は H2(13–15時UTC の高ボラ)を再現する
4. 予想 verdict: m15 = no_directional_structure、m60 = no_directional_structure、
   activity は両方確認 → aggTrades 進行の根拠は「directional からは」出ない

## 8. 実行手順

```sh
uv run python -m mce.research.clock_phase
git add experiments/phase1b && git commit
```

結果の解釈・結論は `docs/findings/` へ台帳化する(本ファイルは変更しない)。
