# 既存 Microstructure v1 資産のレビュー(Phase 7 との対応付け)

- 作成日: 2026-08-16
- 目的: Phase 7(Information-Space Expansion)を始めるにあたり、**既に存在する**
  収集・正規化・品質検査・事前凍結プロトコルを棚卸しし、再利用できる部分と
  欠落している部分を確定する。既存資産を捨てない・重複実装しないための文書。
- 原則: **事前凍結済みプロトコルは結果を見る前に変更しない。**
  本レビューは既存 v1 の定義を一切変更しない(位置付けの整理のみ)。

## 1. 既存資産の一覧

| 資産 | 実体 | 状態 |
|---|---|---|
| 事前凍結プロトコル | [docs/findings/2026-08-16-microstructure-v1-protocol.md](../findings/2026-08-16-microstructure-v1-protocol.md) | **定義凍結・収集中・未検定** |
| 収集仕様 | [docs/microstructure_collection.md](../microstructure_collection.md) | 確定 |
| collector | `src/mce/collect_microstructure.py` | 実装済み(WS 2接続・session rotation・clock quality・REST metadata) |
| quality report | `src/mce/microstructure_quality.py` | 実装済み(gzip/JSON・frame 連番・ACK・lag・板 sequence・約定照合) |
| normalizer | `src/mce/normalize_microstructure.py` | 実装済み(schema v3・immutable shard・contract 換算) |
| 時計品質 | `src/mce/clock_quality.py` | 実装済み(`adjtimex(modes=0)` を60秒ごと) |
| テスト | `tests/test_collect_microstructure.py` / `test_microstructure_quality.py` / `test_normalize_microstructure.py` / `test_clock_quality.py` | 全通過(249件のスイート内) |

収集 channel(OKX、認証不要 production WS):

| 接続 | channel | 得られる情報集合 |
|---|---|---|
| public | `trades` | 集約約定(aggressor side・count・seqId)= Priority A の native 観測 |
| public | `bbo-tbt` | L1 気配と数量(最速10ms)= Priority B |
| public | `books` | 400段 snapshot + 100ms incremental = Priority C |
| public | `instruments` | contract / tick / lot の日中変更(単位換算の正しさ) |
| business | `trades-all` | 個別約定(件数・数量の照合専用) |

## 2. M1 / M2 / M3 の情報 tier 対応

| 仮説 | 内容 | 情報集合 | Phase 7 tier |
|---|---|---|---|
| **M1** | L1 OFI continuation(30秒 horizon) | BBO / L1 | Tier 2(OKX prospective)/ Priority B |
| **M2** | 10bps 表示 depth の net depletion continuation(30秒) | L2 400段 | Tier 2 / Priority C |
| **M3** | organic aggressive-flow absorption reversal(60秒) | trades | Tier 2 / Priority A |

重要な性質の違い(Phase 7 の screening と混同しないこと):

- M1–M3 は **strategy 水準の事前検定**である(固定 horizon・固定執行モデル・
  表示板 VWAP・主コスト 15bps・Holm 補正・pass/kill 条件つき)。
- Phase 7 Tier 0/1 の incremental information test は **情報の存在検定**であり、
  strategy 最適化ではない(入れ子モデル比較・placebo 対照・効果量報告)。
- したがって両者は**目的も判定基準も異なる**。M1–M3 の結果で Phase 7 の設計を変えず、
  Phase 7 の結果で M1–M3 の定義を変えない。

## 3. データ在庫の現状

```sh
uv run python -m mce.data_inventory --json data/analysis/data_inventory.json
```

- `data/` は manifests を除き git 管理外(`.gitignore`)。**本 clone には実データが無い。**
  したがって「どれだけ貯まっているか」は collector を回している実機でのみ確定する。
- 本 clone で確認できる事実:
  - OHLCV / features / labels の manifest: 288,124本・2023-11-19〜2026-08-16・欠損0
  - funding: 280本(2026-05-14〜2026-08-15)
  - open interest: 1,441本(2026-08-10〜2026-08-15)
  - microstructure normalized / raw: **不在**(この環境には無い)
- 実機で最初に確認すべきは、Microstructure v1 §2.1 が要求する
  **24時間 soak → `T0` 登録 → Calibration 60日 → Validation 60日**の進捗であり、
  ラベル検定までに soak を除いて連続120暦日が必要。

## 4. 再利用できるもの / 足りないもの

### 再利用できる(Phase 7 でそのまま使う)

- collector の raw wrapper 契約(`schema_version` / `session_id` / `frame_no` /
  `received_at_ns` / `monotonic_ns`)— event-level 情報の timestamp 規約の雛形
- `books` sequence 状態機械(snapshot/delta・gap 時の無効化)
- quality gate の考え方(**全項目通過前に将来 return を作らない**)
- normalized schema v3 の immutable shard 設計(同一 raw の再正規化で内容一致を検証)
- 単位換算(`ctVal` / `ctMult` / `ctValCcy` / `tickSz` / `lotSz`)
- clock quality の fail-closed 方針
- 統計手順の雛形(day-cluster bootstrap、cluster randomization、Holm 補正、seed 固定)

### 足りない(Phase 7 で新規に必要)

| 欠落 | 影響 | 対応方針 |
|---|---|---|
| **遡及可能な microstructure 履歴** | prospective のみでは最初の検定まで120日超 | Binance Vision の集約系(Tier 0)で先に情報存在検定を行う([expansion protocol §3](information_space_expansion_v1.md)) |
| **derivatives の履歴**(OI 5日 / funding 3ヶ月 / liquidation 未収集) | derivatives 仮説を OKX 履歴で検定できない | Tier 0-B(Binance metrics 5m・2021〜)で screening、OKX は prospective 蓄積を継続 |
| **liquidation データ**(どの venue でも未取得) | 清算カスケード仮説が検証不能 | 取得手段の再調査が先(Binance Vision の該当パスは 2026-08-16 時点 404) |
| bar 集約された aggressive flow の observable | Tier 0-A の feature 生成器が無い | 新規実装(小規模。features 契約に従う) |
| event-level 情報の `available_time` 物理列 | 公開遅延の異なる系列を observable へ昇格できない | data contract §3 の予告どおり、昇格時に導入 |
| cross-venue 同期 | lead-lag 系が扱えない | Tier 3。単一 venue の理解が先 |

## 5. 次に収集すべきデータ(優先順)

1. **継続**: OKX microstructure v1 の prospective 収集(trades / bbo-tbt / books / instruments)。
   これは止めない。止めると再取得不能な履歴が永久に欠落する。
2. **新規・低コスト**: Binance Vision の Tier 0 三点(klines 5m の taker buy 数量・
   metrics 5m の OI と long/short・premiumIndexKlines)。合計 ~30 KB/日で
   2021 年以降の深い履歴が得られる。
3. **条件付き**: Tier 0 で incremental information が確認された機序に限り、
   aggTrades(~5–8 MB/日)へ降りて event-level で機序検証する。
4. **保留**: bookTicker(199 MB/日)、cross-venue、liquidation(取得手段の再調査後)。

## 6. 守る規律

1. Microstructure v1 の `T0` は結果を見て動かさない。
2. quality gate 不通過は「edge なし」ではなく**検定無効**。
3. Validation 不通過仮説の Final 特徴量・損益を計算しない。
4. Phase 7 screening は `ts < 2026-01-01` のみ(venue を問わず Final OOS 期間を封印継承)。
5. 別 venue のデータで得た結論を、OKX 執行を前提とした主張へそのまま格上げしない
   (`replication_class = cross_exchange_validation` として記録する)。
