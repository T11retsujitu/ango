"""Phase 8 — funding 決済 mark の **source resolver**(v1.8.6 草案 §4 / §5 / §8)。

**純粋な解決器だけである。** 取引系列を組み立てず、rho / シグナル / return / PnL /
Layer 1/2/3 実行を一切しない。I/O もしない(入力は呼び出し側が渡す)。

- 入力を**変更しない**(すべて frozen dataclass、内部で複製しない値だけを読む)。
- 同じ入力からは**同じ結果**が出る(`Decimal` と決定的な分岐のみ。乱数も時刻も読まない)。
- **例外で黙って fallback しない。** parse 失敗は「規則で分類された理由」であって、
  握り潰した例外ではない。分類できない入力(未知の status、矛盾した provenance)は
  **送出して止まる**。
- **未適用の草案の実装である。** `phase8_prereg.py` は凍結対象なので触らない。
  定数は当面ここが持ち、v1.8.6 を適用するときに prereg へ移す。

---

## primary 規則(§4。順序は固定。イベントごとに切り替えない)

```text
1. REST markPrice が parse 可能かつ正値
       value    = REST markPrice
       fidelity = exact_rest
       source   = binance_funding_rest

2. REST が absent / unparseable / non-positive で、
   かつ当該バーの mark_open が正値、かつ mark 経路が observed / verified_repair
       value    = mark_open
       fidelity = official_kline_proxy          ← **exact ではない**
       source   = binance_mark_price_kline_open

3. それ以外
       value    = None
       fidelity = unavailable
       resolution_permitted = False             ← §4.1 で layer を中断する
```

**`mark_open` を exact と呼ばない。** 実測の一致率は 1,919 / 2,378 であって 100% ではない
([再構成 probe](../../docs/phase8/funding_mark_reconstruction_probe_v1.md))。

**欠測・stale・route 未確認を補完しない。** 前値の横引きも補間もしない。

## sensitivity 規則(§8)

- **REST が無効なイベントだけ**が対象(REST が有効な決済は sensitivity でも REST のまま)。
- 直前バーの `mark_close` を使う。`observed` / `verified_repair` のときだけ。
- **primary の値に一切影響させない**(別フィールドで持つ)。
- **`open` と直前 `close` の近い方を選ばない。OR 規則を使わない。**
- fidelity 3値を汚さないよう、sensitivity は**自分のラベル**を持つ(§5.1)。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType
import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Final, Mapping, Sequence

from mce.backtest import mark_path
from mce.backtest.splits import FINAL_OOS_START, phase8_layer

UTC = timezone.utc

# --- fidelity(§5)。**primary 系列を記述するラベルである** ------------------
FIDELITY_EXACT_REST: Final = "exact_rest"
FIDELITY_KLINE_PROXY: Final = "official_kline_proxy"
FIDELITY_UNAVAILABLE: Final = "unavailable"
FUNDING_MARK_FIDELITIES: Final = (
    FIDELITY_EXACT_REST, FIDELITY_KLINE_PROXY, FIDELITY_UNAVAILABLE,
)

# --- source -----------------------------------------------------------------
SOURCE_REST: Final = "binance_funding_rest"
SOURCE_KLINE_OPEN: Final = "binance_mark_price_kline_open"
FUNDING_MARK_SOURCE_ORDER: Final = (SOURCE_REST, SOURCE_KLINE_OPEN)

# --- sensitivity(§8 / §5.1)。**fidelity 3値を使わない** --------------------
SENSITIVITY_SOURCE: Final = "binance_mark_price_kline_previous_close"
SENSITIVITY_FIDELITY: Final = "kline_previous_close_sensitivity"
SENSITIVITY_APPLIES_WHEN: Final = "rest_mark_price_invalid"

# --- REST が有効でなかった理由(§4。**「空」と「壊れていた」を混ぜない**)----
REST_ABSENT: Final = "rest_absent"
REST_UNPARSEABLE: Final = "rest_unparseable"
REST_NON_POSITIVE: Final = "rest_non_positive"
REST_INVALID_REASONS: Final = (REST_ABSENT, REST_UNPARSEABLE, REST_NON_POSITIVE)
REST_VALID: Final = "rest_valid"

# --- proxy が使えなかった理由 -----------------------------------------------
PROXY_BAR_MISSING: Final = "mark_bar_missing"
PROXY_OPEN_UNPARSEABLE: Final = "mark_open_unparseable"
PROXY_OPEN_NON_POSITIVE: Final = "mark_open_non_positive"
PROXY_ROUTE_UNVERIFIED: Final = "mark_path_route_unverified"
PROXY_STALE_UNVERIFIED: Final = "mark_path_stale_unverified"
PROXY_SOURCE_UNOBSERVABLE: Final = "mark_path_source_unobservable"
PROXY_BLOCKED_REASONS: Final = (
    PROXY_BAR_MISSING, PROXY_OPEN_UNPARSEABLE, PROXY_OPEN_NON_POSITIVE,
    PROXY_ROUTE_UNVERIFIED, PROXY_STALE_UNVERIFIED, PROXY_SOURCE_UNOBSERVABLE,
)
PROXY_NOT_ATTEMPTED: Final = "proxy_not_attempted_rest_valid"
PROXY_USED: Final = "proxy_used_mark_open"
#: `proxy_reason` が取りうる値の**全体**。テストが自前で文字列を持たないための語彙。
PROXY_REASONS: Final = (*PROXY_BLOCKED_REASONS, PROXY_NOT_ATTEMPTED, PROXY_USED)

# --- sensitivity が使えなかった理由 -----------------------------------------
# **primary の語彙を流用しない。** sensitivity が読むのは直前バーの `mark_close`
# なので、`mark_open_non_positive` と記録すると「どのフィールドを見たのか」が
# 嘘になる(同じ行で mark_open は正なのに、という矛盾した記録が残る)。
SENSITIVITY_BAR_MISSING: Final = "previous_mark_bar_missing"
SENSITIVITY_CLOSE_UNPARSEABLE: Final = "previous_mark_close_unparseable"
SENSITIVITY_CLOSE_NON_POSITIVE: Final = "previous_mark_close_non_positive"
SENSITIVITY_ROUTE_UNVERIFIED: Final = "previous_mark_path_route_unverified"
SENSITIVITY_STALE_UNVERIFIED: Final = "previous_mark_path_stale_unverified"
SENSITIVITY_SOURCE_UNOBSERVABLE: Final = "previous_mark_path_source_unobservable"
SENSITIVITY_BLOCKED_REASONS: Final = (
    SENSITIVITY_BAR_MISSING, SENSITIVITY_CLOSE_UNPARSEABLE,
    SENSITIVITY_CLOSE_NON_POSITIVE, SENSITIVITY_ROUTE_UNVERIFIED,
    SENSITIVITY_STALE_UNVERIFIED, SENSITIVITY_SOURCE_UNOBSERVABLE,
)
SENSITIVITY_USED: Final = "sensitivity_used_previous_mark_close"
#: REST が有効なので sensitivity を試していない(§8: REST 有効なら REST のまま)
SENSITIVITY_NOT_ATTEMPTED: Final = "sensitivity_not_attempted_rest_valid"
SENSITIVITY_REASONS: Final = (
    *SENSITIVITY_BLOCKED_REASONS, SENSITIVITY_NOT_ATTEMPTED, SENSITIVITY_USED,
)

#: §32 の許容外 status → proxy 不能理由。**遮断を source の欠測に化けさせない。**
_STATUS_TO_BLOCKED_REASON: Final[Mapping[str, str]] = MappingProxyType({
    mark_path.ROUTE_UNVERIFIED: PROXY_ROUTE_UNVERIFIED,
    mark_path.STALE_UNVERIFIED: PROXY_STALE_UNVERIFIED,
    mark_path.SOURCE_UNOBSERVABLE: PROXY_SOURCE_UNOBSERVABLE,
})

# --- layer ごとの区分(§6)。**ハードコードした結果ではなく規則で導く** ------
LAYER_FIDELITY_EXACT: Final = "exact_rest"
LAYER_FIDELITY_PARTIAL: Final = "partial_proxy"
LAYER_FIDELITY_UNKNOWN: Final = "unknown_until_observed"
LAYER_FIDELITY_CLASSES: Final = (
    LAYER_FIDELITY_EXACT, LAYER_FIDELITY_PARTIAL, LAYER_FIDELITY_UNKNOWN,
)

#: 草案 §6 / §9 の短縮名 → `splits.phase8_layer()` が返す正式名。
#: **2つの語彙が併存しているので、明示的に橋渡しする。** 橋渡しが無いと
#: `layer_fidelity_for("layer1", ...)` が**黙って** `unknown_until_observed` を返し、
#: 「観測が無い」と「名前が違う」が区別できなくなる。
LAYER_ALIASES: Final[Mapping[str, str]] = {
    "layer1": "literature_in_sample",
    "layer2": "contaminated_confirmation",
    "layer3": "phase8_prospective_final",
    "layer_x": "phase8_contaminated",
}
#: 表に必ず並べる layer。**layer 3 を表から落とさない**(§6)。
#: 観測が無い layer は `unknown_until_observed` として**行が残る**。
CANONICAL_LAYERS: Final = (
    "literature_in_sample",
    "contaminated_confirmation",
    "phase8_contaminated",
    "phase8_prospective_final",
)

#: proxy は exact ではない。**この値を True にする経路を作らない。**
FUNDING_MARK_PROXY_IS_EXACT: Final = False
FUNDING_MARK_OR_RULE_ALLOWED: Final = False
FUNDING_MARK_NEAREST_CANDIDATE_ALLOWED: Final = False
FUNDING_MARK_INTERPOLATION: Final = "none"


class FundingMarkResolverError(ValueError):
    """入力が規則で分類できない。**黙って fallback しない。**"""


@dataclass(frozen=True)
class MarkBarInput:
    """**canonical タイムライン上の1行**(§32)。値は原文の文字列で受け取る。

    parquet の `Float64` を経路に入れないため、価格は文字列で渡す
    (Decimal 完全一致の判定と同じ理由。丸めの持ち込みを防ぐ)。

    **呼び出し規約**: §32 の canonical タイムラインは**欠測バーも行として残す**。
    したがって Vision に行が無いバーでも、**状態を持った行**をここへ渡すこと
    (`mark_open_text=None`, `mark_path_status="route_unverified"` 等)。

    - そうすれば「なぜ proxy を使えなかったのか」が
      `mark_path_route_unverified` のように**状態として残る**。
      §6 が要求する「`route_unverified` であって `source_unobservable` ではない」
      という区別は、この行が渡されて初めて出力に現れる。
    - `mark_bar=None` は「**canonical タイムラインに行そのものが無い**」を意味する
      (タイムラインの範囲外)。値が無いだけの欠測を `None` で表さない。
    """

    ts: datetime
    mark_path_status: str
    mark_open_text: str | None = None
    mark_close_text: str | None = None


@dataclass(frozen=True)
class FundingMarkInputs:
    """1 決済ぶんの入力。**resolver はこれを変更しない。**"""

    #: Vision canonical の決済時刻(= `calc_time`)
    ts: datetime
    #: REST 照合行(matched でない行を渡さないこと)
    rest_funding_time: datetime | None = None
    rest_mark_price_text: str | None = None
    #: `floor_5m(ts)` のバーと、その直前のバー
    mark_bar: MarkBarInput | None = None
    previous_mark_bar: MarkBarInput | None = None
    #: 決済レート(原文)。resolver は値を使わず provenance として持ち回るだけ
    funding_rate_text: str | None = None


@dataclass(frozen=True)
class FundingMarkResolution:
    """解決結果。**primary と sensitivity は別フィールドで持つ。**"""

    ts: datetime
    layer: str
    primary_value: Decimal | None
    primary_source: str | None
    primary_fidelity: str
    primary_reason: str
    rest_validity: str
    proxy_reason: str
    sensitivity_value: Decimal | None
    sensitivity_source: str | None
    sensitivity_fidelity: str | None
    sensitivity_reason: str
    rest_funding_time: datetime | None
    mark_bar_ts: datetime | None
    previous_mark_bar_ts: datetime | None
    funding_rate_text: str | None
    mark_path_status: str | None
    previous_mark_path_status: str | None
    resolution_permitted: bool
    #: sensitivity が**適用対象だったか**(§8: REST が無効な決済だけが対象)。
    #: `sensitivity_value is None` だけでは「適用外」と「適用対象だが使えない」が
    #: 区別できず、下流が §8.1 の gate を黙って無効化しうる。
    sensitivity_applicable: bool = False
    trace: tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict:
        """決定的な JSON 表現。**同じ入力なら同じバイト列になる。**"""
        return {
            "ts": self.ts.isoformat(),
            "layer": self.layer,
            "primary_value": None if self.primary_value is None else str(self.primary_value),
            "primary_source": self.primary_source,
            "primary_fidelity": self.primary_fidelity,
            "primary_reason": self.primary_reason,
            "rest_validity": self.rest_validity,
            "proxy_reason": self.proxy_reason,
            "sensitivity_value": (
                None if self.sensitivity_value is None else str(self.sensitivity_value)
            ),
            "sensitivity_source": self.sensitivity_source,
            "sensitivity_fidelity": self.sensitivity_fidelity,
            "sensitivity_reason": self.sensitivity_reason,
            "rest_funding_time": (
                None if self.rest_funding_time is None else self.rest_funding_time.isoformat()
            ),
            "mark_bar_ts": None if self.mark_bar_ts is None else self.mark_bar_ts.isoformat(),
            "previous_mark_bar_ts": (
                None if self.previous_mark_bar_ts is None
                else self.previous_mark_bar_ts.isoformat()
            ),
            "funding_rate_text": self.funding_rate_text,
            "mark_path_status": self.mark_path_status,
            "previous_mark_path_status": self.previous_mark_path_status,
            "resolution_permitted": self.resolution_permitted,
            "sensitivity_applicable": self.sensitivity_applicable,
            "proxy_is_exact": FUNDING_MARK_PROXY_IS_EXACT,
            "or_rule_used": FUNDING_MARK_OR_RULE_ALLOWED,
            "nearest_candidate_selection": FUNDING_MARK_NEAREST_CANDIDATE_ALLOWED,
            "interpolation": FUNDING_MARK_INTERPOLATION,
            "fidelity_describes": "primary_selection_only",
            "proxy_bar_rule": PROXY_BAR_RULE,
            "trace": list(self.trace),
        }


#: 受け入れる十進表記。**Python の `Decimal` 構文をそのまま使わない。**
#: `Decimal("1_0")` は 10 になり、`Decimal(" 7 ")` は 7 になる。壊れた文字列が
#: 「有効な値」として通ると、**data corruption が exact_rest として採用される**。
_DECIMAL_TEXT: Final = re.compile(r"^[+-]?(\d+(\.\d*)?|\.\d+)([eE][+-]?\d+)?$")


def _parse_positive(text: str | None) -> tuple[Decimal | None, str]:
    """原文 → (値, 分類)。**分類は規則であって、握り潰した例外ではない。**

    分類: `"absent"` / `"unparseable"` / `"non_positive"` / `"valid"`。

    空白は「値が無い」と見なすが、**値の内側の空白やアンダースコアは
    `unparseable`** である(`" 7 "` は 7 だが `"1_0"` は壊れた入力)。
    """
    if text is None:
        return None, "absent"
    if not isinstance(text, str):
        # parquet の Float64 や Decimal をそのまま渡された場合。**規則で拒否する**
        # (素の AttributeError にすると、呼び出し側が分類できない)。
        raise FundingMarkResolverError(
            f"価格は原文の文字列で渡すこと(丸めを持ち込まないため): {type(text).__name__}"
        )
    stripped = text.strip()
    if not stripped:
        return None, "absent"
    if not _DECIMAL_TEXT.match(stripped):
        return None, "unparseable"
    try:
        value = Decimal(stripped)
    except InvalidOperation:
        return None, "unparseable"
    if not value.is_finite():
        # inf / NaN は数値として parse できても値として使えない
        return None, "unparseable"
    if value <= 0:
        return value, "non_positive"
    return value, "valid"


def _require_known_status(status: str) -> bool:
    """§32 の凍結された状態語彙で検査する。**未知の status は送出して止まる。**"""
    try:
        return mark_path.is_acceptable(status)
    except ValueError as exc:  # 凍結語彙に無い
        raise FundingMarkResolverError(str(exc)) from exc


def _check_seal(ts: datetime) -> None:
    if ts.tzinfo is None:
        raise FundingMarkResolverError(f"tz-naive な決済時刻は受け付けない: {ts!r}")
    if ts >= FINAL_OOS_START:
        raise FundingMarkResolverError(
            f"封印域の決済を渡している: {ts.isoformat()} >= {FINAL_OOS_START.isoformat()}"
        )


#: proxy に使うバーの選び方(§4)。**任意のバーを受け取らない。**
PROXY_BAR_RULE: Final = "floor_5m(funding_time)"
BAR = timedelta(minutes=5)
#: REST 照合行が同じ決済を指していることの許容差。照合表(v1.8.5 の実測)と同じ 1 秒。
REST_FUNDING_TIME_TOLERANCE: Final = timedelta(seconds=1)


def floor_5m(ts: datetime) -> datetime:
    """その時刻を含む5分バーの開始時刻。境界ちょうどは**新しいバー**。"""
    epoch_ms = int(ts.timestamp() * 1000)
    floored = (epoch_ms // 300_000) * 300_000
    return datetime.fromtimestamp(floored / 1000, UTC)


def _check_bar_alignment(
    ts: datetime, bar: MarkBarInput | None, previous: MarkBarInput | None
) -> None:
    """渡されたバーが §4 / §8 の規則どおりの位置にあるか検査する。

    **任意のバーを黙って受け取らない。** 位置がずれた入力を受け入れると、
    「次のバー(未来)を proxy にした」「同一バーの close を sensitivity にした」が
    resolver の外で起こり、規則が守られているかを誰も検証できなくなる。
    """
    expected = floor_5m(ts)
    if bar is not None and bar.ts != expected:
        raise FundingMarkResolverError(
            f"proxy バーの位置が {PROXY_BAR_RULE} と違う: "
            f"{bar.ts.isoformat()} != {expected.isoformat()}"
        )
    if previous is not None and previous.ts != expected - BAR:
        raise FundingMarkResolverError(
            "sensitivity は**直前バー**でなければならない: "
            f"{previous.ts.isoformat()} != {(expected - BAR).isoformat()}"
        )


def _check_rest_provenance(ts: datetime, rest_funding_time: datetime | None) -> None:
    """REST 行が**この決済の行**であることを検査する。

    存在するだけでは足りない。別の決済の REST 行を渡されて `exact_rest` に
    なってしまうと、照合表の許容差規則が resolver の手前で無効化される。
    """
    if rest_funding_time is None:
        raise FundingMarkResolverError(
            "REST markPrice があるのに rest_funding_time が無い(照合行の provenance が欠けている)"
        )
    if abs(rest_funding_time - ts) > REST_FUNDING_TIME_TOLERANCE:
        raise FundingMarkResolverError(
            f"REST 行が別の決済を指している: {rest_funding_time.isoformat()} vs {ts.isoformat()} "
            f"(許容差 {REST_FUNDING_TIME_TOLERANCE})"
        )


def resolve(inputs: FundingMarkInputs) -> FundingMarkResolution:
    """1 決済ぶんの funding mark を解決する(§4 / §8)。

    **純粋関数。** `inputs` を変更せず、I/O をせず、時刻も乱数も読まない。
    """
    _check_seal(inputs.ts)
    trace: list[str] = [f"seal_ok:{inputs.ts.isoformat()}"]

    bar = inputs.mark_bar
    previous = inputs.previous_mark_bar
    _check_bar_alignment(inputs.ts, bar, previous)
    bar_acceptable = _require_known_status(bar.mark_path_status) if bar else None
    previous_acceptable = (
        _require_known_status(previous.mark_path_status) if previous else None
    )

    # --- step 1: REST -------------------------------------------------------
    rest_value, rest_class = _parse_positive(inputs.rest_mark_price_text)
    if rest_class != "absent":
        # **値があるなら出所も要る。** 空(absent)は規則どおり step 2 へ落ちるので、
        # ここで provenance を要求しない(要求すると §4 step 2 が塞がる)。
        _check_rest_provenance(inputs.ts, inputs.rest_funding_time)
    trace.append(f"rest:{rest_class}")

    if rest_class == "valid":
        trace.append(f"primary:{FIDELITY_EXACT_REST}")
        trace.append(f"sensitivity:{PROXY_NOT_ATTEMPTED}")
        return FundingMarkResolution(
            ts=inputs.ts,
            layer=phase8_layer(inputs.ts),
            primary_value=rest_value,
            primary_source=SOURCE_REST,
            primary_fidelity=FIDELITY_EXACT_REST,
            primary_reason=REST_VALID,
            rest_validity=REST_VALID,
            proxy_reason=PROXY_NOT_ATTEMPTED,
            # **REST が有効な決済は sensitivity でも REST のまま**(§8)。
            sensitivity_value=None,
            sensitivity_source=None,
            sensitivity_fidelity=None,
            sensitivity_reason=SENSITIVITY_NOT_ATTEMPTED,
            rest_funding_time=inputs.rest_funding_time,
            mark_bar_ts=bar.ts if bar else None,
            previous_mark_bar_ts=previous.ts if previous else None,
            funding_rate_text=inputs.funding_rate_text,
            mark_path_status=bar.mark_path_status if bar else None,
            previous_mark_path_status=previous.mark_path_status if previous else None,
            resolution_permitted=True,
            sensitivity_applicable=False,
            trace=tuple(trace),
        )

    rest_invalid_reason = {
        "absent": REST_ABSENT,
        "unparseable": REST_UNPARSEABLE,
        "non_positive": REST_NON_POSITIVE,
    }[rest_class]

    # --- step 2: proxy(REST が有効でないときだけ)---------------------------
    proxy_value, proxy_reason = _proxy_candidate(bar, bar_acceptable)
    trace.append(f"proxy:{proxy_reason}")

    # --- sensitivity(REST が無効なイベントだけが対象。§8)-------------------
    sensitivity_value, sensitivity_reason = _sensitivity_candidate(
        previous, previous_acceptable
    )
    trace.append(f"sensitivity:{sensitivity_reason}")

    if proxy_value is not None:
        fidelity, source, permitted = FIDELITY_KLINE_PROXY, SOURCE_KLINE_OPEN, True
    else:
        fidelity, source, permitted = FIDELITY_UNAVAILABLE, None, False
    trace.append(f"primary:{fidelity}")

    return FundingMarkResolution(
        ts=inputs.ts,
        layer=phase8_layer(inputs.ts),
        primary_value=proxy_value,
        primary_source=source,
        primary_fidelity=fidelity,
        # **REST が無効だった理由を失わない。** proxy が使えたかどうかとは別に残す。
        primary_reason=rest_invalid_reason,
        rest_validity=rest_invalid_reason,
        proxy_reason=proxy_reason,
        sensitivity_value=sensitivity_value,
        sensitivity_source=SENSITIVITY_SOURCE if sensitivity_value is not None else None,
        sensitivity_fidelity=(
            SENSITIVITY_FIDELITY if sensitivity_value is not None else None
        ),
        sensitivity_reason=sensitivity_reason,
        rest_funding_time=inputs.rest_funding_time,
        mark_bar_ts=bar.ts if bar else None,
        previous_mark_bar_ts=previous.ts if previous else None,
        funding_rate_text=inputs.funding_rate_text,
        mark_path_status=bar.mark_path_status if bar else None,
        previous_mark_path_status=previous.mark_path_status if previous else None,
        resolution_permitted=permitted,
        sensitivity_applicable=True,
        trace=tuple(trace),
    )


def _proxy_candidate(
    bar: MarkBarInput | None, acceptable: bool | None
) -> tuple[Decimal | None, str]:
    """当該バーの `mark_open` を proxy として使えるか(§4 step 2)。

    **状態が許容外なら値を見ない。** 「使える値があるのに status が悪い」場合でも
    使わないことを、順序で保証する(横引き・補間をしないため)。
    """
    if bar is None:
        return None, PROXY_BAR_MISSING
    # **状態を値より先に見る。** 逆にすると「使える値があるから使う」経路ができ、
    # 横引き・補間の禁止が壊れる。状態由来の理由はここで保存される。
    if not acceptable:
        return None, _STATUS_TO_BLOCKED_REASON[bar.mark_path_status]
    value, kind = _parse_positive(bar.mark_open_text)
    if kind == "unparseable":
        return None, PROXY_OPEN_UNPARSEABLE
    if kind == "absent":
        # 行はあるが価格が無い = **バーのデータが欠測している**(§32 の欠測行)
        return None, PROXY_BAR_MISSING
    if kind == "non_positive":
        return None, PROXY_OPEN_NON_POSITIVE
    return value, PROXY_USED


#: 直前バーの状態 → sensitivity が使えなかった理由。**primary と別語彙。**
_STATUS_TO_SENSITIVITY_REASON: Final[Mapping[str, str]] = MappingProxyType({
    mark_path.ROUTE_UNVERIFIED: SENSITIVITY_ROUTE_UNVERIFIED,
    mark_path.STALE_UNVERIFIED: SENSITIVITY_STALE_UNVERIFIED,
    mark_path.SOURCE_UNOBSERVABLE: SENSITIVITY_SOURCE_UNOBSERVABLE,
})


def _sensitivity_candidate(
    previous: MarkBarInput | None, acceptable: bool | None
) -> tuple[Decimal | None, str]:
    """直前バーの `mark_close`(§8)。**primary には一切影響しない。**

    理由の語彙は primary と分けてある。読んでいるのは `mark_close` なので、
    `mark_open_*` と記録すると**どのフィールドを見たのかが嘘になる**。
    """
    if previous is None:
        return None, SENSITIVITY_BAR_MISSING
    if not acceptable:
        return None, _STATUS_TO_SENSITIVITY_REASON[previous.mark_path_status]
    value, kind = _parse_positive(previous.mark_close_text)
    if kind == "unparseable":
        return None, SENSITIVITY_CLOSE_UNPARSEABLE
    if kind == "absent":
        return None, SENSITIVITY_BAR_MISSING
    if kind == "non_positive":
        return None, SENSITIVITY_CLOSE_NON_POSITIVE
    return value, SENSITIVITY_USED


def resolve_all(inputs: Sequence[FundingMarkInputs]) -> tuple[FundingMarkResolution, ...]:
    """複数決済をまとめて解決する。**順序を保つ。相互に影響させない。**"""
    return tuple(resolve(i) for i in inputs)


def layer_fidelity(resolutions: Sequence[FundingMarkResolution]) -> dict:
    """layer ごとの fidelity 区分を**規則から導く**(§6)。

    **表をハードコードして返さない。** 観測された fidelity から分類する:

    ```text
    観測 0 件                    → unknown_until_observed
    全件が exact_rest            → exact_rest
    それ以外                     → partial_proxy
    ```

    したがって **layer 3 は「まだ1件も無い」ので `unknown_until_observed`** になる。
    **exact だと推定しない。** データが現れたら実測で分類し直される。

    `partial_proxy` は「一様に exact_rest ではない」という意味であって、
    proxy が必ず存在することを意味しない。だから**件数を必ず併記する**
    (全件 unavailable の layer も件数を見れば区別できる)。

    **`CANONICAL_LAYERS` は観測が無くても必ず行として並ぶ**(§6)。
    表から消すと、後から「layer 3 は exact だと決まっていた」と読まれうる。
    """
    buckets: dict[str, dict[str, int]] = {
        layer: {f: 0 for f in FUNDING_MARK_FIDELITIES} for layer in CANONICAL_LAYERS
    }
    for resolution in resolutions:
        counts = buckets.setdefault(
            resolution.layer, {f: 0 for f in FUNDING_MARK_FIDELITIES}
        )
        counts[resolution.primary_fidelity] += 1

    alias_of = {canonical: short for short, canonical in LAYER_ALIASES.items()}
    out: dict[str, dict] = {}
    for layer, counts in sorted(buckets.items()):
        total = sum(counts.values())
        if total == 0:
            klass = LAYER_FIDELITY_UNKNOWN
        elif counts[FIDELITY_EXACT_REST] == total:
            klass = LAYER_FIDELITY_EXACT
        else:
            klass = LAYER_FIDELITY_PARTIAL
        out[layer] = {
            "class": klass,
            # 草案 §6 / §9 の短縮名も併記する(2語彙の取り違えを防ぐ)
            "alias": alias_of.get(layer),
            "events": total,
            "counts": dict(counts),
            "shares": {
                f: (f"{counts[f]}/{total}" if total else "0/0")
                for f in FUNDING_MARK_FIDELITIES
            },
        }
    return out


def resolve_layer_name(layer: str) -> str:
    """草案の短縮名(`layer1` …)も `phase8_layer()` の正式名も受ける。

    **未知の名前は送出して止まる。** 黙って `unknown_until_observed` を返すと、
    「観測が無い」と「名前を間違えた」が区別できなくなる。
    """
    if layer in LAYER_ALIASES:
        return LAYER_ALIASES[layer]
    if layer in CANONICAL_LAYERS:
        return layer
    raise FundingMarkResolverError(
        f"未知の layer 名: {layer!r}(既知: {sorted({*LAYER_ALIASES, *CANONICAL_LAYERS})})"
    )


def layer_fidelity_for(
    layer: str, resolutions: Sequence[FundingMarkResolution]
) -> str:
    """ある layer の区分。**観測が無ければ `unknown_until_observed`。**

    短縮名(`layer1` / `layer2` / `layer3`)でも引ける。
    **未知の名前は送出する**(`resolve_layer_name`)。
    """
    canonical = resolve_layer_name(layer)
    return layer_fidelity(resolutions)[canonical]["class"]
