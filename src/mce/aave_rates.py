"""Phase 8.1 — Aave variable borrow APR の履歴アダプタ(**入力データ再構成のみ**)。

凍結プロトコル **v1.8.4** §27 / §30 の **partial proxy** を実装する。

    RATE_SOURCE_FIDELITY = "partial_proxy_not_exact_A2"

**A2 の厳密再現ではない。** A2 は版・network・market・提供元を書いていない(§26)。

経済的な source of truth は **Ethereum mainnet 上の Aave コントラクト state** であり、
archive RPC は **transport にすぎない**(§30.1)。

    RATE_SOURCE_OF_TRUTH = "aave_contract_state_on_ethereum_mainnet"
    RATE_ACCESS_ROUTE    = "archive_rpc_eth_call"

日次の手順(§27.4 + §30.2):

1. 対象時刻 = その UTC 日の 00:00:00Z
2. `block.timestamp <= target` を満たす**最新**の Ethereum ブロックを解決する
3. **そのブロックを指定して**履歴 state を照会する
4. **同じブロック**で、世代に応じた reserve list primitive を読む
   (V1: `getReserves()` / V2: `getReservesList()` / V3 Core: `getReservesList()`)
5. USDT / USDC / DAI の**凍結アドレス3つすべて**がその list の member であることを要求する
6. member であるものについて `variableBorrowRate` を取る
7. 生値を **RAY 単位の APR** と解釈し `raw / 1e27` で小数へ変換する
8. **3資産すべて**が揃わなければ **null**。0 で代替しない。2資産へ縮めない。
   前世代を延長しない。凍結 splice 日を動かさない。補間も forward-fill もしない。
9. 3つの小数 APR を**等加重平均**する
10. **平滑化も補間もしない**

**borrow rate を frontend の APY へ変換しない。** APR のまま使う。

**launch 直後の高い金利を filter/clip/smooth/winsorize しない**(§30.3 = O1)。
有効な非ゼロ観測は、値がどれほど極端でもそのまま残す。

全語ゼロ構造体の検出は **独立した cross-check としてのみ**残す(§30.2)。
membership と食い違ったら **integrity error を出して値を出さない**。
どちらか一方の解釈を選ぶことはしない。

**本モジュールは rho を計算せず、シグナルも損益も生成しない。**
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Final

from mce import phase8_prereg as P

UTC = timezone.utc
RAY: Final = 10**27

#: Ethereum mainnet。全観測に記録し、違えば integrity error(§30.1)。
ETHEREUM_MAINNET_CHAIN_ID: Final = 1

__all__ = [
    "AaveGeneration",
    "GENERATIONS",
    "TokenSpec",
    "RpcError",
    "ReserveReading",
    "ReserveListReading",
    "DailyObservation",
    "ETHEREUM_MAINNET_CHAIN_ID",
    "generation_for",
    "chain_id_of",
    "resolve_block_at_or_before",
    "read_reserve_list",
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
#
# `reserve_list_selector` も**世代ごとに実測して確定した**(§30.2):
#     V1      : getReserves()      = 0x0902f1ac
#     V2      : getReservesList()  = 0xd1946dbc
#     V3 Core : getReservesList()  = 0xd1946dbc
# V1 だけ primitive 名が違うため、**共通名で呼べると仮定してはならない**。
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TokenSpec:
    """資産の同定。**ティッカーだけで同定しない**(§27 決定ログ)。"""

    symbol: str
    address: str
    decimals: int

    @property
    def key(self) -> str:
        return self.address.lower()


@dataclass(frozen=True)
class AaveGeneration:
    name: str
    pool_address: str
    rate_word_index: int
    expected_word_count: int
    reserve_list_selector: str
    reserve_list_signature: str
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

_GET_RESERVES: Final = "0x0902f1ac"  # getReserves()      — V1
_GET_RESERVES_LIST: Final = "0xd1946dbc"  # getReservesList()  — V2 / V3
_GET_RESERVE_DATA: Final = "0x35ea6a75"  # getReserveData(address)

GENERATIONS: Final[tuple[AaveGeneration, ...]] = (
    AaveGeneration(
        name="aave_v1",
        pool_address="0x398eC7346DcD622eDc5ae82352F02bE94C62d119",  # LendingPool
        rate_word_index=5,
        expected_word_count=13,
        reserve_list_selector=_GET_RESERVES,
        reserve_list_signature="getReserves()",
        start=datetime(2020, 1, 8, tzinfo=UTC),
        end=datetime(2020, 12, 3, tzinfo=UTC),
        tokens=_STABLES,
    ),
    AaveGeneration(
        name="aave_v2",
        pool_address="0x7d2768dE32b0b80b7a3454c06BdAc94A69DDc7A9",  # LendingPool
        rate_word_index=4,
        expected_word_count=12,
        reserve_list_selector=_GET_RESERVES_LIST,
        reserve_list_signature="getReservesList()",
        start=datetime(2020, 12, 3, tzinfo=UTC),
        end=datetime(2023, 1, 27, tzinfo=UTC),
        tokens=_STABLES,
    ),
    AaveGeneration(
        name="aave_v3_core",
        pool_address="0x87870Bca3F3fD6335C3F4ce8392D69350B4fA4E2",  # Pool (Core)
        rate_word_index=4,
        expected_word_count=15,
        reserve_list_selector=_GET_RESERVES_LIST,
        reserve_list_signature="getReservesList()",
        start=datetime(2023, 1, 27, tzinfo=UTC),
        end=None,  # V4 へ移行しない(§27.2)
        tokens=_STABLES,
    ),
)

# 既定の archive RPC 候補。**認証不要で履歴 eth_call が通ることを実測したもの**。
# これらは **transport であって経済的な source ではない**(§30.1)。
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


def _decode_address_array(hex_result: str) -> tuple[str, ...]:
    """ABI の `address[]` を復号する。offset を読み飛ばして長さと要素を取る。"""
    words = _words(hex_result)
    if len(words) < 2:
        raise RpcError("address[] として復号できない(語数不足)")
    offset = words[0] // 32
    if offset >= len(words):
        raise RpcError("address[] の offset が範囲外")
    count = words[offset]
    body = words[offset + 1 : offset + 1 + count]
    if len(body) != count:
        raise RpcError("address[] の要素数が宣言と合わない")
    return tuple("0x%040x" % w for w in body)


def chain_id_of(endpoint: str) -> int:
    """endpoint が指す chain id。**mainnet 以外を黙って使わない**(§30.1)。"""
    parsed, _ = _rpc(endpoint, "eth_chainId", [])
    return int(parsed["result"], 16)


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
    endpoint: str,
    target_ts: datetime,
    *,
    cache: dict[int, int] | None = None,
    hint: int | None = None,
) -> tuple[int, int, str]:
    """`block.timestamp <= target` を満たす**最新**ブロックを解決する。

    戻り値 `(block_number, block_timestamp, block_hash)`。
    **target より後のブロックは決して返さない**(未来参照の禁止)。

    `hint` は直前の日で解決したブロック番号。**答えを変えない純粋な高速化**で、
    与えても与えなくても同じ `(n, ts)` に収束する:最後に `ts(n) <= target` と
    `ts(n+1) > target` の両方を明示的に確認して返すためである。
    """
    target = int(target_ts.timestamp())
    cache = {} if cache is None else cache

    def ts_of(n: int) -> int:
        if n not in cache:
            cache[n] = _block_timestamp(endpoint, n)
        return cache[n]

    parsed, _ = _rpc(endpoint, "eth_blockNumber", [])
    head = int(parsed["result"], 16)
    if ts_of(head) < target:
        raise RpcError("target が chain head より新しい")
    if ts_of(1) > target:
        raise RpcError("target が genesis より古い")

    # ---- bracket を作る。hint があれば近傍から、無ければ全域から。
    if hint is None:
        lo, hi = 1, head
    else:
        seed = min(max(hint, 1), head)
        if ts_of(seed) <= target:
            lo, hi = seed, None
            step = max(1, (target - ts_of(seed)) // 12 + 1)
            probe = min(head, seed + step)
            while hi is None:
                if ts_of(probe) > target:
                    hi = probe
                else:
                    lo = probe
                    if probe == head:
                        hi = head  # ts(head) >= target は上で保証済み
                    else:
                        step = max(1, step * 2)
                        probe = min(head, probe + step)
        else:
            hi, lo = seed, None
            step = max(1, (ts_of(seed) - target) // 12 + 1)
            probe = max(1, seed - step)
            while lo is None:
                if ts_of(probe) <= target:
                    lo = probe
                else:
                    hi = probe
                    if probe == 1:
                        lo = 1  # ts(1) <= target は上で保証済み
                    else:
                        step = max(1, step * 2)
                        probe = max(1, probe - step)

    # ---- bracket 内を二分探索
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if ts_of(mid) <= target:
            lo = mid
        else:
            hi = mid - 1

    parsed, _ = _rpc(endpoint, "eth_getBlockByNumber", [hex(lo), False])
    block = parsed["result"]
    got = int(block["timestamp"], 16)
    if got > target:  # pragma: no cover - 探索の不変条件
        raise RpcError("解決したブロックが target より後になった")
    if lo < head and ts_of(lo + 1) <= target:  # pragma: no cover - 同上
        raise RpcError("解決したブロックが最新ではない")
    return lo, got, block["hash"]


# ---------------------------------------------------------------------------
# reserve list の読み取り(§30.2 = H17 の解決)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReserveListReading:
    """観測ブロックにおける「初期化済み reserve list」。"""

    signature: str
    addresses: tuple[str, ...]  # すべて小文字
    count: int | None
    response_sha256: str | None
    error: str | None = None

    def contains(self, token: TokenSpec) -> bool:
        return token.key in self.addresses


def read_reserve_list(
    endpoint: str, gen: AaveGeneration, block_number: int
) -> ReserveListReading:
    """**rate と同じ履歴ブロック**で、その世代の reserve list を読む。"""
    try:
        parsed, raw = _rpc(
            endpoint,
            "eth_call",
            [{"to": gen.pool_address, "data": gen.reserve_list_selector}, hex(block_number)],
        )
    except RpcError as exc:
        return ReserveListReading(gen.reserve_list_signature, (), None, None, str(exc))
    result = parsed.get("result") or "0x"
    if result == "0x":
        return ReserveListReading(
            gen.reserve_list_signature, (), None, _sha256(raw),
            "空応答(このブロックに reserve list が存在しない)",
        )
    try:
        addrs = _decode_address_array(result)
    except RpcError as exc:
        return ReserveListReading(gen.reserve_list_signature, (), None, _sha256(raw), str(exc))
    return ReserveListReading(
        gen.reserve_list_signature, tuple(a.lower() for a in addrs), len(addrs), _sha256(raw)
    )


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
    def zero_struct_state(self) -> str:
        """全語ゼロ診断の三値。**独立した cross-check 専用**(§30.2)。

        - ``"uninitialised"``: 期待語数を返したが**全語ゼロ**
        - ``"initialised"``  : 期待語数を返し、非ゼロ語がある
        - ``"unreadable"``   : 空応答 / 語数不一致 / RPC 失敗

        **この診断は完全性判定の権威ではない。** 権威は reserve list membership。
        両者が食い違ったら integrity error を出し、どちらかを選ぶことはしない。
        """
        if self.word_count is None or self.word_count == 0 or self.error is not None:
            return "unreadable"
        return "uninitialised" if self.nonzero_word_count == 0 else "initialised"

    @property
    def reserve_uninitialised(self) -> bool:
        return self.zero_struct_state == "uninitialised"


def read_variable_borrow_rate(
    endpoint: str, gen: AaveGeneration, token: TokenSpec, block_number: int
) -> ReserveReading:
    """指定ブロックの `variableBorrowRate` を RAY 生値と小数 APR で返す。

    **APY へ変換しない。** `raw / 1e27` の APR のままである。
    **値の clip / smoothing / winsorize は一切しない**(§30.3)。
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
    """1日分の観測と provenance(§27.6 + §30.1)。"""

    date_utc: str
    target_ts: str
    generation: str | None
    network: str
    chain_id: int | None
    market: str
    block_number: int | None
    block_timestamp: int | None
    block_hash: str | None
    endpoint: str
    access_route: str
    reserve_list: ReserveListReading | None
    members_present: tuple[str, ...]
    missing_reserves: tuple[str, ...]
    components: tuple[ReserveReading, ...]
    mean_apr: float | None
    integrity_error: str | None
    source_fidelity: str
    retrieved_at_utc: str
    note: str | None = None
    uninitialised_reserves: tuple[str, ...] = ()

    @property
    def complete(self) -> bool:
        return self.mean_apr is not None

    @property
    def basket_size(self) -> int:
        """平均に使った資産数。**2資産 fallback は存在しない**(3 か 0)。"""
        return len(P.RATE_ASSETS) if self.mean_apr is not None else 0


def _integrity_conflicts(
    gen: AaveGeneration, lst: ReserveListReading, readings: tuple[ReserveReading, ...]
) -> list[str]:
    """membership と全語ゼロ診断の**食い違い**を列挙する(§30.2)。

    片方が決定的でない場合(list が読めない / 構造体が読めない)は食い違いを
    主張しない。その日は単に欠測になる。
    """
    if lst.error is not None:
        return []
    conflicts = []
    by_symbol = {r.symbol: r for r in readings}
    for token in gen.tokens:
        reading = by_symbol.get(token.symbol)
        if reading is None:
            continue
        state = reading.zero_struct_state
        member = lst.contains(token)
        if state == "unreadable":
            continue
        if member and state == "uninitialised":
            conflicts.append(
                f"{token.symbol}: reserve list は member と言うが構造体は全語ゼロ"
            )
        elif not member and state == "initialised":
            conflicts.append(
                f"{token.symbol}: reserve list は非 member と言うが構造体は初期化済み"
            )
    return conflicts


def daily_observation(
    endpoint: str,
    day: datetime,
    *,
    cache: dict[int, int] | None = None,
    hint: int | None = None,
    chain_id: int | None = None,
) -> DailyObservation:
    """その UTC 日の 00:00:00Z 時点の3資産平均 APR と provenance。

    **3資産が同一ブロックの初期化済み reserve list の member でなければ null。**
    0 で代替しない / 2資産へ縮めない / 前世代を延長しない / 補間しない。
    """
    target = day.astimezone(UTC).replace(hour=P.RATE_SNAPSHOT_HOUR_UTC, minute=0,
                                         second=0, microsecond=0)
    now = datetime.now(UTC).isoformat()
    gen = generation_for(target)
    base = dict(
        date_utc=target.date().isoformat(), target_ts=target.isoformat(),
        network=P.RATE_MARKET_NETWORK, chain_id=chain_id, market=P.RATE_MARKET_INSTANCE,
        endpoint=endpoint, access_route=P.RATE_ACCESS_ROUTE,
        source_fidelity=P.RATE_SOURCE_FIDELITY, retrieved_at_utc=now,
    )
    empty = dict(
        block_number=None, block_timestamp=None, block_hash=None, reserve_list=None,
        members_present=(), components=(), mean_apr=None,
    )
    all_symbols = tuple(t.symbol for t in _STABLES)

    if chain_id is not None and chain_id != ETHEREUM_MAINNET_CHAIN_ID:
        return DailyObservation(
            generation=None, missing_reserves=all_symbols,
            integrity_error=f"chain id が mainnet ではない: {chain_id}",
            note="chain id 不一致のため観測を破棄", **empty, **base,
        )
    if gen is None:
        return DailyObservation(
            generation=None, missing_reserves=all_symbols, integrity_error=None,
            note="V1 稼働開始前(または V4 期を除外)", **empty, **base,
        )
    try:
        number, block_ts, block_hash = resolve_block_at_or_before(
            endpoint, target, cache=cache, hint=hint
        )
    except RpcError as exc:
        return DailyObservation(
            generation=gen.name, missing_reserves=all_symbols, integrity_error=None,
            note=f"ブロック解決に失敗: {exc}", **empty, **base,
        )

    # ---- 権威: **rate と同じブロック**での reserve list membership
    lst = read_reserve_list(endpoint, gen, number)
    members = tuple(t.symbol for t in gen.tokens if lst.contains(t))
    missing = tuple(t.symbol for t in gen.tokens if not lst.contains(t))

    readings = tuple(
        read_variable_borrow_rate(endpoint, gen, tok, number) for tok in gen.tokens
    )
    # ---- 独立した cross-check(権威ではない)
    uninit = tuple(r.symbol for r in readings if r.reserve_uninitialised)
    conflicts = _integrity_conflicts(gen, lst, readings)

    located = dict(block_number=number, block_timestamp=block_ts, block_hash=block_hash,
                   reserve_list=lst, members_present=members, components=readings)

    if conflicts:
        # **どちらかの解釈を選ばない。** 値を出さない。
        return DailyObservation(
            generation=gen.name, missing_reserves=missing, mean_apr=None,
            integrity_error="; ".join(conflicts),
            note="membership と全語ゼロ診断が不一致のため値を出さない",
            uninitialised_reserves=uninit, **located, **base,
        )
    if lst.error is not None:
        return DailyObservation(
            generation=gen.name, missing_reserves=all_symbols, mean_apr=None,
            integrity_error=None, note=f"reserve list を読めない: {lst.error}",
            uninitialised_reserves=uninit, **located, **base,
        )
    if missing:
        return DailyObservation(
            generation=gen.name, missing_reserves=missing, mean_apr=None,
            integrity_error=None,
            note=f"初期化済み reserve list に不在: {', '.join(missing)}(0 で代替しない)",
            uninitialised_reserves=uninit, **located, **base,
        )

    aprs = [r.apr_decimal for r in readings]
    unreadable = tuple(r.symbol for r in readings if r.apr_decimal is None)
    if unreadable:
        return DailyObservation(
            generation=gen.name, missing_reserves=unreadable, mean_apr=None,
            integrity_error=None,
            note=f"member だが rate を読めない: {', '.join(unreadable)}",
            uninitialised_reserves=uninit, **located, **base,
        )
    mean = sum(aprs) / len(aprs)  # type: ignore[arg-type]
    return DailyObservation(
        generation=gen.name, missing_reserves=(), mean_apr=mean, integrity_error=None,
        note=None, uninitialised_reserves=uninit, **located, **base,
    )
