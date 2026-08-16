"""Arm C の LLM interface(凍結: docs/phase3/bakeoff_protocol.md §10)。

責務は「masking したプロンプトを組み立て、structured output で仮説レコードを受け取り、
全リクエスト/レスポンスを記録する」ことのみ。AST 化は plan.py、評価は runner.py。

masking(§10.2): 銘柄・取引所・暦日付・価格水準をプロンプトから除去し、
LLM の parametric hindsight(R13/R14/R15)を構造的に抑制する。

determinism: LLM 呼び出しは決定的ではない(temperature は Claude Opus 5 以降で
API から削除されており、seed も存在しない)。代わりに全応答を記録して replay 可能にする。
"""

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from mce.search import grammar
from mce.search.plan import HYPOTHESIS_SCHEMA

DEFAULT_MODEL = "claude-opus-5"
MAX_TOKENS = 16000
MASKED_SYMBOL = "ASSET_X"

MASKING = {
    "symbol": f"BTC-USDT-SWAP -> {MASKED_SYMBOL}",
    "venue": "exchange name withheld",
    "calendar": "dates replaced by bar indices",
    "price_level": "price levels withheld",
}

SYSTEM_PROMPT = f"""You are a quantitative research assistant proposing falsifiable trading \
hypotheses for {MASKED_SYMBOL}, a perpetual futures contract on a major digital asset, sampled \
as 5-minute bars.

You do not know which asset, which venue, or which calendar period this is, and you must not \
speculate about any of these. Reason only from market-structure mechanisms (order flow, \
liquidity, participant behaviour, periodic activity), never from remembered price history or \
named historical events. Any hypothesis that depends on knowing the era or the asset's realised \
path is invalid.

You do not write code. You output structured hypothesis records; a deterministic compiler turns \
your dsl_plan into an executable strategy and a frozen evaluator scores it. You cannot change the \
evaluator, the costs, or the data splits."""

_FEATURE_DOC = """Available features (all computed from bars up to and including the signal bar;
window is in 5-minute bars):
  return(w)      close/close[-w] - 1
  trend(w)       close / SMA_w(close) - 1
  volatility(w)  stdev of 5m returns over the last w bars
  range(w)       (max(high,w) - min(low,w)) / close
  volume_z(w)    (volume - mean_w) / stdev_w, window excludes the current bar
  ma_slope(w)    SMA_w(t) / SMA_w(t-1) - 1"""


def _mechanism_brief() -> str:
    return f"""Execution and cost rules (frozen, not negotiable):
  - A signal formed at the close of bar t is filled at the OPEN of bar t+1.
  - Round-trip transaction cost is 10 bps of notional (taker). A strategy must earn more than
    that per round trip to have any net edge. Assume nothing about slippage beyond this.
  - Positions are +1 / 0 / -1 with fixed size; no compounding.

Structural budget (violations are rejected without consuming evaluation budget):
  - at most 4 distinct features, at most 6 numeric parameters, AST depth at most 5.
  - Parameter count: each condition costs 2 (window + threshold); a clock costs 1;
    persistence_bars costs 1; holding_bars costs 1. Example: entry + one filter + holding = 5.
  - side "both" DUPLICATES the whole condition tree (long and short each carry it), so it doubles
    the cost of the entry and every filter: side "both" with one filter costs 8 and is rejected.
    With side "both", use the entry alone (4) plus at most one of clock / persistence / holding.
    Use side "long" or "short" when you want filters.
  - At least one market feature is required. A pure clock rule with no market feature is rejected.

Menus (values outside these are snapped or rejected):
  window:           {list(grammar.WINDOW_MENU)}
  persistence_bars: {list(grammar.HOLDS_MENU)} or null
  holding_bars:     {list(grammar.HOLDING_MENU)} or null
  clock:            period 15 or 60, phase a multiple of 5 below the period, or null
  thresholds are quantised to a fixed magnitude menu per feature, so give a realistic magnitude
  (returns and trend around 0.0005-0.005; ma_slope around 0.0001-0.001; volatility 0.0005-0.005;
  range 0.002-0.02; volume_z 0.5-3.0) rather than an arbitrary number."""


def build_user_prompt(n: int, history: list[dict], bars_research: int) -> str:
    """1ラウンド分のプロンプト。history は research primary metrics のみ(§10.3)。"""
    parts = [
        f"The research window is a contiguous block of {bars_research} five-minute bars, "
        f"referred to only by bar index. No calendar information is available to you.",
        "",
        _FEATURE_DOC,
        "",
        _mechanism_brief(),
    ]
    if history:
        parts += ["", "Results of your previous proposals on the research window "
                  "(research metrics only; validation results are withheld by design):"]
        for h in history:
            parts.append(
                f"  {h['hypothesis_id']} family={h['signal_family']} "
                f"trades={h['trades']} net_bps_per_trade={h['net_bps_per_trade']} "
                f"sharpe={h['sharpe']} turnover={h['turnover']} -> {h['outcome']}"
            )
        parts.append(
            "\nUse these outcomes. Do not repeat a mechanism that already failed for a reason "
            "your new proposal does not address, and do not re-send a plan you have sent before."
        )
    parts += [
        "",
        f"Propose {n} NEW falsifiable hypotheses. Each must name a mechanism that could plausibly "
        "produce a persistent, cost-surviving edge, and state the failure mode you expect to kill "
        "it. Prefer proposals that differ from one another in mechanism, not just in parameters.",
    ]
    return "\n".join(parts)


def prompt_hash(system: str, user: str) -> str:
    return hashlib.sha256((system + "\n\x00\n" + user).encode()).hexdigest()


class LlmError(RuntimeError):
    pass


AUTH_HELP = """Anthropic API の認証情報が見つからない。次のいずれかを設定すること:

  1) API キー(console.anthropic.com で発行)
       export ANTHROPIC_API_KEY=sk-ant-...
  2) OAuth プロファイル(ant CLI。SDK が自動で読む)
       ant auth login     # 確認: ant auth status

注意: 空文字の ANTHROPIC_API_KEY が export されていると 1) と誤認して失敗する。
その場合は unset ANTHROPIC_API_KEY してから設定し直すこと。"""


class AuthError(LlmError):
    """API 認証情報が解決できない。"""


class Refusal(LlmError):
    """safety classifier がリクエストを拒否した(stop_reason == "refusal")。"""


@dataclass
class AnthropicClient:
    """Anthropic Messages API 経由の仮説生成(structured output)。

    temperature は Claude Opus 5 以降で API から削除されているため設定しない。
    したがって同一プロンプトでも応答は変わりうる(§10.4)。
    """

    model: str = DEFAULT_MODEL
    max_tokens: int = MAX_TOKENS
    _client: object | None = field(default=None, repr=False)

    def __post_init__(self):
        if self._client is None:
            try:
                import anthropic
            except ImportError as e:  # pragma: no cover - 環境依存
                raise LlmError("anthropic SDK が必要: uv add anthropic") from e
            self._client = anthropic.Anthropic()

    def propose(self, system: str, user: str) -> dict:
        try:
            response = self._client.messages.create(
                model=self.model,
                max_tokens=self.max_tokens,
                system=system,
                messages=[{"role": "user", "content": user}],
                output_config={"format": {"type": "json_schema", "schema": HYPOTHESIS_SCHEMA}},
            )
        except TypeError as e:  # SDK は認証情報が解決できないとき TypeError を投げる
            if "authentication" in str(e).lower():
                raise AuthError(AUTH_HELP) from e
            raise
        if response.stop_reason == "refusal":
            details = getattr(response, "stop_details", None)
            raise Refusal(f"model declined the request (category={getattr(details, 'category', None)})")
        text = next((b.text for b in response.content if b.type == "text"), None)
        if not text:
            raise LlmError(f"テキスト応答が空(stop_reason={response.stop_reason})")
        return json.loads(text)


@dataclass
class Transcript:
    """LLM 呼び出しの追記専用記録(replay の入力になる)。"""

    path: Path

    def __post_init__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def record(self, entry: dict) -> None:
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n")

    def rounds(self) -> list[dict]:
        if not self.path.exists():
            return []
        return [json.loads(line) for line in self.path.read_text(encoding="utf-8").splitlines() if line.strip()]


def replay_hypotheses(transcript_path: Path) -> list[dict]:
    """記録済み transcript から仮説レコード列を復元する(API 呼び出しなし・決定的)。"""
    out: list[dict] = []
    for entry in Transcript(transcript_path).rounds():
        if entry.get("status") == "ok":
            out.extend(entry.get("response", {}).get("hypotheses", []))
    return out


ProposeFn = Callable[[str, str], dict]
