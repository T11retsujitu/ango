"""Phase 8.1 — Aave variable borrow APR の履歴アダプタ(**入力データ再構成のみ**)。

凍結プロトコル v1.8.3 §27 の **partial proxy** を実装する。

    RATE_SOURCE_FIDELITY = "partial_proxy_not_exact_A2"

**A2 の厳密再現ではない。** A2 は版・network・market・提供元を書いていない(§26)。

日次の手順(§27.4):

1. 対象時刻 = その UTC 日の 00:00:00Z
2. `block.timestamp <= target` を満たす**最新**の Ethereum ブロックを解決する
3. **そのブロックを指定して**履歴 state を照会する
4. USDT / USDC / DAI ちょうど3資産の `variableBorrowRate` を取る
5. 生値を **RAY 単位の APR** と解釈し `raw / 1e27` で小数へ変換する
6. **3資産すべてを要求**する。1つでも欠ければ **null**
7. 3つの小数 APR を**等加重平均**する
8. **平滑化も補間もしない**

**borrow rate を frontend の APY へ変換しない。** APR のまま使う。

**本モジュールは rho を計算せず、シグナルも損益も生成しない。**
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Final, Iterable, Mapping

from mce import phase8_prereg as P

UTC = timezone.utc
RAY: Final = 10**27

__all__ = [
    "AaveGeneration",
    "GENERATIONS",
    "TokenSpec",
    "RpcError",
    "ReserveReading",
    "DailyObservation",
    "generation_for",
    "resolve_block_at_or_before",
    "read_variable_borrow_rate",
    "daily_observation",
]

# ---------------------------------------------------------------------------
# 凍結された世代マッピング(§27.2)。**V4 へは移行しない。**
#
# `rate_word_index` は **世代ごとに実測して確定した**(do not identify by position
# alone)。V2 と V3 はどちらも index 4 だが、**構造体の並びが違う**ため偶然一致して
# いるだけである:
#     V1: [4]=liquidityRate [5]=variableBorrowRate [6]=stableBorrowRate
#     V2: [1]=liquidityIndex [2]=variableBorrowIndex [3]=liquidityRate [4]=variableBorrowRate
#     V3: [1]=liquidityIndex [2]=liquidityRate      [3]=variableBorrowIndex [4]=variableBorrowRate
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TokenSpec:
    """資産の同定。**ティッカーだけで同定しない**(§27 決定ログ)。"""

    symbol: str
    address: str
    decimals: int


@dataclass(frozen=True)
class AaveGeneration:
    name: str
    pool_address: str
    rate_word_index: int
    expected_word_count: int
    start: datetime
    end: datetime | None  # None = 以降ずっと
    tokens: tuple[TokenSpec, ...]

    def covers(self, ts: datetime) -> bool:
        return ts >= self.start and (self.end is None or ts < self.end)


# Ethereum mainnet の ERC-20。世代ごとに**別々に凍結して検証する**。
_USDT = TokenSpec("USDT", "0xdAC17F958D2ee523a2206206994597C13D831ec7", 6)
_USDC = TokenSpec("USDC", "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48", 6)
_DAI = TokenSpec("DAI", "0x6B175474E89094C44Da98b954EedeAC495271d0F", 18)
_STABLES: Final = (_USDT, _USDC, _DAI)

GENERATIONS: Final[tuple[AaveGeneration, ...]] = (
    AaveGeneration(
        name="aave_v1",
        pool_address="0x398eC7346DcD622eDc5ae82352F02bE94C62d119",  # LendingPool
        rate_word_index=5,
        expected_word_count=13,
        start=datetime(2020, 1, 8, tzinfo=UTC),
        end=datetime(2020, 12, 3, tzinfo=UTC),
        tokens=_STABLES,
    ),
    AaveGeneration(
        name="aave_v2",
        pool_address="0x7d2768dE32b0b80b7a3454c06BdAc94A69DDc7A9",  # LendingPool
        rate_word_index=4,
        expected_word_count=12,
        start=datetime(2020, 12, 3, tzinfo=UTC),
        end=datetime(2023, 1, 27, tzinfo=UTC),
        tokens=_STABLES,
    ),
    AaveGeneration(
        name="aave_v3_core",
        pool_address="0x87870Bca3F3fD6335C3F4ce8392D69350B4fA4E2",  # Pool (Core)
        rate_word_index=4,
        expected_word_count=15,
        start=datetime(2023, 1, 27, tzinfo=UTC),
        end=None,  # V4 へ移行しない(§27.2)
        tokens=_STABLES,
    ),
)

_GET_RESERVE_DATA: Final = "0x35ea6a75"  # getReserveData(address)

# 既定の archive RPC 候補。**認証不要で履歴 eth_call が通ることを実測したもの**。
DEFAULT_RPC_ENDPOINTS: Final = (
    "https://eth-mainnet.public.blastapi.io",
    "https://eth.merkle.io",
    "https://rpc.mevblocker.io",
    "https://gateway.tenderly.co/public/mainnet",
)


class RpcError(RuntimeError):
    """RPC が結果を返さなかった。**欠測として扱い、値を捏造しない。**"""


# ---------------------------------------------------------------------------
# RPC(依存を増やさないため curl を使う)
# ---------------------------------------------------------------------------


def _rpc(endpoint: str, method: str, params: list, timeout: int = 40) -> tuple[dict, str]:
    payload = json.dumps({"jsonrpc": "2.0", "method": method, "params": params, "id": 1})
    proc = subprocess.run(
        ["curl", "-s", "--max-time", str(timeout), "-X", "POST",
         "-H", "Content-Type: application/json", "-d", payload, endpoint],
        capture_output=True, text=True,
    )
    raw = proc.stdout
    if not raw.strip():
        raise RpcError(f"{method}: 空応答({endpoint})")
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:  # pragma: no cover - ネットワーク依存
        raise RpcError(f"{method}: JSON でない応答({endpoint})") from exc
    if "error" in parsed:
        raise RpcError(f"{method}: {parsed['error']}({endpoint})")
    return parsed, raw


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _words(hex_result: str) -> list[int]:
    body = hex_result[2:]
    return [int(body[i : i + 64], 16) for i in range(0, len(body), 64)]


# ---------------------------------------------------------------------------
# 世代の解決とブロック解決
# ---------------------------------------------------------------------------


def generation_for(ts: datetime) -> AaveGeneration | None:
    """その時刻に適用する世代。V1 稼働前は None。**V4 は返さない。**"""
    for gen in GENERATIONS:
        if gen.covers(ts):
            return gen
    return None


def _block_timestamp(endpoint: str, number: int) -> int:
    parsed, _ = _rpc(endpoint, "eth_getBlockByNumber", [hex(number), False])
    result = parsed.get("result")
    if not result:
        raise RpcError(f"block {number} が見つからない")
    return int(result["timestamp"], 16)


def resolve_block_at_or_before(
    endpoint: str, target_ts: datetime, *, cache: dict[int, int] | None = None
) -> tuple[int, int, str]:
    """`block.timestamp <= target` を満たす**最新**ブロックを二分探索で解決する。

    戻り値 `(block_number, block_timestamp, block_hash)`。
    **target より後のブロックは決して返さない**(未来参照の禁止)。
    """
    target = int(target_ts.timestamp())
    cache = {} if cache is None else cache

    def ts_of(n: int) -> int:
        if n not in cache:
            cache[n] = _block_timestamp(endpoint, n)
        return cache[n]

    parsed, _ = _rpc(endpoint, "eth_blockNumber", [])
    hi = int(parsed["result"], 16)
    if ts_of(hi) < target:
        raise RpcError("target が chain head より新しい")
    lo = 1
    if ts_of(lo) > target:
        raise RpcError("target が genesis より古い")
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if ts_of(mid) <= target:
            lo = mid
        else:
            hi = mid - 1
    parsed, _ = _rpc(endpoint, "eth_getBlockByNumber", [hex(lo), False])
    block = parsed["result"]
    got = int(block["timestamp"], 16)
    if got > target:  # pragma: no cover - 二分探索の不変条件
        raise RpcError("解決したブロックが target より後になった")
    return lo, got, block["hash"]


# ---------------------------------------------------------------------------
# reserve の読み取り
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReserveReading:
    symbol: str
    token_address: str
    raw_ray: int | None
    apr_decimal: float | None
    word_count: int | None
    response_sha256: str | None
    error: str | None = None
    nonzero_word_count: int | None = None

    @property
    def reserve_uninitialised(self) -> bool:
        """構造体が**全語ゼロ**か。=その世代にその reserve がまだ上場していない。

        **診断専用のフラグであり、凍結された完全性規則を変更しない**(§27.4 の
        「3資産すべてを要求、欠ければ null」は *読み取りの成否* で判定される)。
        未上場 reserve は成功応答として 0 を返すため、凍結規則では **0% として
        平均に混入する**。この矛盾は H17 として報告のみ行い、ここでは解決しない。
        """
        return self.word_count is not None and self.word_count > 0 and self.nonzero_word_count == 0


def read_variable_borrow_rate(
    endpoint: str, gen: AaveGeneration, token: TokenSpec, block_number: int
) -> ReserveReading:
    """指定ブロックの `variableBorrowRate` を RAY 生値と小数 APR で返す。

    **APY へ変換しない。** `raw / 1e27` の APR のままである。
    """
    data = _GET_RESERVE_DATA + "0" * 24 + token.address[2:].lower()
    try:
        parsed, raw = _rpc(
            endpoint, "eth_call", [{"to": gen.pool_address, "data": data}, hex(block_number)]
        )
    except RpcError as exc:
        return ReserveReading(token.symbol, token.address, None, None, None, None, str(exc))
    result = parsed.get("result") or "0x"
    if result == "0x":
        return ReserveReading(
            token.symbol, token.address, None, None, 0, _sha256(raw),
            "空応答(この世代/ブロックに reserve が存在しない)", 0,
        )
    words = _words(result)
    if len(words) != gen.expected_word_count:
        return ReserveReading(
            token.symbol, token.address, None, None, len(words), _sha256(raw),
            f"word 数が凍結値と違う: {len(words)} != {gen.expected_word_count}",
            sum(1 for w in words if w != 0),
        )
    raw_ray = words[gen.rate_word_index]
    return ReserveReading(
        token.symbol, token.address, raw_ray, raw_ray / RAY, len(words), _sha256(raw),
        None, sum(1 for w in words if w != 0),
    )


# ---------------------------------------------------------------------------
# 日次観測
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DailyObservation:
    """1日分の観測と provenance(§27.6)。"""

    date_utc: str
    target_ts: str
    generation: str | None
    network: str
    market: str
    block_number: int | None
    block_timestamp: int | None
    block_hash: str | None
    endpoint: str
    components: tuple[ReserveReading, ...]
    mean_apr: float | None
    source_fidelity: str
    retrieved_at_utc: str
    note: str | None = None
    uninitialised_reserves: tuple[str, ...] = ()

    @property
    def complete(self) -> bool:
        return self.mean_apr is not None

    @property
    def contaminated_by_uninitialised(self) -> bool:
        """未上場 reserve の 0% が平均へ混入しているか(**H17 の検出フラグ**)。

        凍結規則(§27.4)を変更せずに **記録だけ** する。ここで欠測扱いへ倒すと
        凍結された完全性規則を黙って書き換えることになるため、行わない。
        """
        return self.mean_apr is not None and bool(self.uninitialised_reserves)


def daily_observation(
    endpoint: str, day: datetime, *, cache: dict[int, int] | None = None
) -> DailyObservation:
    """その UTC 日の 00:00:00Z 時点の3資産平均 APR と provenance。

    3資産のいずれかが欠ければ `mean_apr` は **None**(補完しない)。
    """
    target = day.astimezone(UTC).replace(hour=P.RATE_SNAPSHOT_HOUR_UTC, minute=0,
                                         second=0, microsecond=0)
    now = datetime.now(UTC).isoformat()
    gen = generation_for(target)
    base = dict(
        date_utc=target.date().isoformat(), target_ts=target.isoformat(),
        network=P.RATE_MARKET_NETWORK, market=P.RATE_MARKET_INSTANCE, endpoint=endpoint,
        source_fidelity=P.RATE_SOURCE_FIDELITY, retrieved_at_utc=now,
    )
    if gen is None:
        return DailyObservation(
            generation=None, block_number=None, block_timestamp=None, block_hash=None,
            components=(), mean_apr=None, note="V1 稼働開始前(または V4 期を除外)", **base,
        )
    try:
        number, block_ts, block_hash = resolve_block_at_or_before(endpoint, target, cache=cache)
    except RpcError as exc:
        return DailyObservation(
            generation=gen.name, block_number=None, block_timestamp=None, block_hash=None,
            components=(), mean_apr=None, note=f"ブロック解決に失敗: {exc}", **base,
        )
    readings = tuple(
        read_variable_borrow_rate(endpoint, gen, tok, number) for tok in gen.tokens
    )
    aprs = [r.apr_decimal for r in readings]
    mean = None
    if P.RATE_BASKET_REQUIRE_ALL and all(a is not None for a in aprs) and len(aprs) == 3:
        mean = sum(aprs) / len(aprs)  # type: ignore[arg-type]
    # H17: 未上場 reserve は**成功応答として全語ゼロ**を返す。凍結規則はこれを
    # 「読めた」と判定するため 0% が平均へ入る。値は凍結どおりのまま、
    # 事実だけ provenance に残す。
    uninit = tuple(r.symbol for r in readings if r.reserve_uninitialised)
    note = None if mean is not None else "3資産が揃わないため null(補完しない)"
    if mean is not None and uninit:
        note = f"H17: 未初期化 reserve が 0% として平均に混入({', '.join(uninit)})"
    return DailyObservation(
        generation=gen.name, block_number=number, block_timestamp=block_ts,
        block_hash=block_hash, components=readings, mean_apr=mean,
        note=note, uninitialised_reserves=uninit, **base,
    )
