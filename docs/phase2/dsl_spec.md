# Phase 2 — Semantic Schema + DSL + AST 仕様 v0(実装対象。凍結は Phase 3 開始前)

- 作成日: 2026-08-16
- 実装: `src/mce/dsl/`
- 参照: ROADMAP §Phase 2、[freeze_v1](../phase1/freeze_v1.md)(Judge は凍結済み・変更不可)、
  Hubble(DSL/AST/deterministic evaluation)、AlphaSchema(semantic space)
- 位置づけ: searcher(Random / Genetic / LLM)は **Python を書けない**。
  生成できるのは本仕様の AST(JSON)のみで、whitelist compiler が凍結済み Judge の
  strategy callable へ決定的に変換する。

## 1. 設計原則

1. **観測可能性は構造で保証する**: 全 op は「バー t の close までに確定した値」しか
   参照できない。負の lag・未来参照 op は存在しない(grammar に無いものは書けない)。
2. **欠損バー規約は Data Contract に従う**: window は ts 基準で本数完全性を要求し、
   不足・欠損は null。null な条件は**取引しない**(flat)に解決される。
3. **whitelist 実行**: compiler は本仕様の op 表のみ解釈する。arbitrary Python /
   file access / dynamic import / network / final_oos アクセスは構造的に不可能。
4. **決定的**: 同一 AST × 同一データ → bit 一致。乱数なし。
5. **正規化 hash**: AST は正規形(キー順序・可換演算子の子順序を正規化)の
   sha256 を持ち、Phase 4 の duplicate control の基礎になる。

## 2. 型システム

- `num`: float 系列(feature・数値 transform の出力)
- `bool`: 論理系列(比較・論理演算の出力。null は「不明」= 最終的に flat)
- ノードは JSON object: `{"op": <name>, ...args}`。子ノードは値、パラメータは数値。

## 3. Op 一覧

### 3.1 Feature(num を返す葉)

| op | 形式 | 定義(全て close[t] 時点で確定) | window範囲 |
|---|---|---|---|
| `return` | `{"op":"return","window":w}` | close(t) / close(t−w bar) − 1。**ts一致join**(欠損バー跨ぎはnull) | 1–288 |
| `trend` | `{"op":"trend","window":w}` | close(t) / SMA_w(close) − 1(現在バー含む・w本完全時のみ) | 2–288 |
| `volatility` | `{"op":"volatility","window":w}` | 直近w本の5分リターン標準偏差(現在バー含む・w本完全) | 2–288 |
| `range` | `{"op":"range","window":w}` | (max(high,w) − min(low,w)) / close(t)(現在バー含む) | 2–288 |
| `volume_z` | `{"op":"volume_z","window":w}` | (volume(t) − mean) / std、mean/stdは**現在バーを除く**直近w本(closed="left")。std=0はnull | 2–288 |
| `ma_slope` | `{"op":"ma_slope","window":w}` | SMA_w(t) / SMA_w(t−1 bar) − 1(前barはts一致join) | 2–288 |

### 3.2 Transform(num → num)

| op | 形式 | 定義 |
|---|---|---|
| `rolling_mean` | `{"op":"rolling_mean","x":<num>,"window":w}` | 直近w本平均(現在バー含む・完全窓) |
| `rolling_std` | `{"op":"rolling_std","x":<num>,"window":w}` | 直近w本標準偏差(同上) |
| `zscore` | `{"op":"zscore","x":<num>,"window":w}` | (x − rolling_mean) / rolling_std(同上。std=0はnull) |

### 3.3 Bool

| op | 形式 | 定義 |
|---|---|---|
| `greater` | `{"op":"greater","x":<num>,"threshold":c}` | x > c(x null → null) |
| `less` | `{"op":"less","x":<num>,"threshold":c}` | x < c |
| `and` / `or` | `{"op":"and","a":<bool>,"b":<bool>}` | Kleene論理(nullは伝播、最終段でflat化)。可換: hash正規化で子を順序化 |
| `not` | `{"op":"not","a":<bool>}` | 否定(null → null) |
| `clock_is` | `{"op":"clock_is","period":p,"phase":q}` | minute(ts) % p == q。p ∈ {15, 60}、q は 5 の倍数で 0 ≤ q < p。ROADMAP の `clock_phase(period)` は比較演算と組むと冗長なため bool 形へ変更(Phase 1B の clock 構造を Context として使う用途に合わせた) |
| `holds_for` | `{"op":"holds_for","a":<bool>,"bars":n}` | 条件が直近n本**連続で**成立(欠損バーを跨ぐ場合は不成立=null)。Quality「persistence」の実装であり、Phase 1A の断片化問題への対応(エントリー安定化)。n: 2–48 |

## 4. Strategy ルート

```json
{
  "type": "strategy",
  "long_if":        <bool node> | null,
  "short_if":       <bool node> | null,
  "flat_if":        <bool node> | null,
  "abstain_unless": <bool node> | null,
  "max_holding_bars": int | null
}
```

評価順序(凍結対象のセマンティクス):

1. `long` = long_if(null→False)、`short` = short_if(null→False)
2. target = +1 (long ∧ ¬short) / −1 (short ∧ ¬long) / 0(両立・どちらも偽)
   — **矛盾は flat**(決定的)
3. `flat_if` 成立行は 0 で上書き
4. `abstain_unless` があれば、成立行以外は 0
5. `max_holding_bars` は ExecutionConfig へ渡す(凍結済み執行エンジンの強制exit)

long_if / short_if の少なくとも一方は必須。出力は {-1, 0, +1} の Int8 系列で、
凍結済み `run_backtest` にそのまま入る。

## 5. 制約(ROADMAP §2.3 準拠)

| 制約 | 値 | 数え方 |
|---|---|---|
| `max_ast_depth` | 5 | 葉=1、各ノード=1+max(子)。root条件ごとに測り最大値 |
| `max_features` | 4 | **相異なる** feature ノード(op+パラメータ)の数 |
| `max_parameters` | 6 | 数値パラメータ総数(window / threshold / phase / bars / max_holding_bars)。period は構造扱いで数えない |
| `max_holding_bars` | ≤ 48 | |
| window 上限 | 288(1日) | warmup を有界に保つ |

違反は `DslValidationError`(検証は compile 前に必ず実行)。未知の op・型不一致
(bool位置にnum等)・範囲外パラメータも同様。

追加制約: **feature ノードが最低1つ必要**。市場状態を一切参照しない strategy
(例: clock_is 単独の無条件時刻売買)は不可 — Quarter-Hour 論文の
「Do not copy: quarter-hourで無条件売買する strategy」に対応する構造的禁止。

なお、feature 1つにつき window + 比較 threshold の最低2パラメータを要するため、
実効的には max_parameters(6)が先に束縛し、max_features(4)は上限として余裕を持つ。

## 6. 正規化と hash

- 正規形: パラメータキーをソートし、`and`/`or` の2子を子の hash 順に並べ替える
- `ast_hash` = 正規形 JSON(sort_keys)の sha256
- 同一戦略の表記揺れ(and の左右交換など)は同一 hash に潰れる
  → Phase 4 duplicate control の第1層

## 7. Semantic Schema(語彙)

AlphaSchema 流の探索空間。LLM / searcher の仮説レコードはこの語彙のみ使用できる:

- **Event**: momentum / reversal / volatility_shock / volume_shock / breakout / clock_boundary
- **Context**: high_volatility / low_volatility / trend / range / high_volume / low_volume
- **Quality**: persistence / acceleration / exhaustion / divergence / confirmation
- **Direction**: continuation / reversal
- **Action**: long / short / flat / abstain / exit

仮説レコード(ROADMAP §Arm C の形式)は `schema.validate_hypothesis` が検証する。
必須: hypothesis_id / event / context(list) / quality(list) / direction / action /
hypothesis(自由文) / expected_failure_mode。語彙外の値は拒否。

hypothesis → AST の変換は Phase 3(searcher 側)の責務。Phase 2 は
「語彙の定義」と「ASTの検証・コンパイル」まで。

## 8. v2 候補(今回入れない)

- enter/exit 二重閾値(本格ヒステリシス)— `holds_for` で不足なら
- feature 間比較(greater(x, y))— パラメータでなく系列同士
- funding / OI 系 feature(データ蓄積後)
- `abstain_unless` の連続量版(confidence gating)

## 9. 凍結計画

Phase 3(search bakeoff)開始前に本仕様を v1 として凍結する。凍結後は
op の追加・制約変更を searcher 実行中に行わない(行う場合は bakeoff やり直し)。
