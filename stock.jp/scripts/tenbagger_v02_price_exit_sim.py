#!/usr/bin/env python3
"""Apply the price-observable v0.2 exit rules to known survivor episodes.

This is deliberately not a full v0.2 strategy backtest. The input contains
ex-post tenbagger winners among current listings and has no point-in-time score,
fundamental, disclosure, delisting, or portfolio data. The simulation is useful
for auditing the mechanics and opportunity cost of the price-only exit rules.
"""

from __future__ import annotations

import argparse
import bisect
import csv
import json
import statistics
from dataclasses import dataclass
from datetime import date
from pathlib import Path

try:
    from scripts.tenbagger_price_scan import Price, add_years, parse_prices
except ModuleNotFoundError:  # Direct execution: python scripts/<file>.py
    from tenbagger_price_scan import Price, add_years, parse_prices


DEFAULT_EPISODES = Path("data/tenbagger-survivor-price-episodes-2016-2026.csv")
DEFAULT_CACHE_DIR = Path(".cache/yahoo-daily")
DEFAULT_OUTPUT = Path("data/tenbagger-v0.2-price-only-pnl-2016-2026.csv")
DEFAULT_REPORT = Path("docs/research/tenbagger-v0.2-price-only-pnl-2016-2026.md")
DEFAULT_CALENDAR_CODE = "7203"


@dataclass(frozen=True)
class Trade:
    rule: str
    trigger_day: date
    execution_day: date
    fraction_of_q0: float
    execution_price: float


@dataclass(frozen=True)
class SimulationResult:
    code: str
    name: str
    label: str
    evaluation_day: date
    entry_day: date
    entry_price: float
    latest_day: date
    latest_price: float
    trades: tuple[Trade, ...]
    pending_rule: str
    pending_trigger_day: date | None
    remaining_fraction: float

    @property
    def realized_cash_multiple(self) -> float:
        return sum(
            trade.fraction_of_q0 * trade.execution_price / self.entry_price
            for trade in self.trades
        )

    @property
    def terminal_mark_multiple(self) -> float:
        return self.remaining_fraction * self.latest_price / self.entry_price

    @property
    def gross_value_multiple(self) -> float:
        return self.realized_cash_multiple + self.terminal_mark_multiple

    @property
    def gross_pnl_pct(self) -> float:
        return (self.gross_value_multiple - 1) * 100

    @property
    def buy_hold_multiple(self) -> float:
        return self.latest_price / self.entry_price

    @property
    def buy_hold_pnl_pct(self) -> float:
        return (self.buy_hold_multiple - 1) * 100


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--episodes", type=Path, default=DEFAULT_EPISODES)
    parser.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE_DIR)
    parser.add_argument("--calendar-code", default=DEFAULT_CALENDAR_CODE)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    return parser.parse_args()


def load_prices(path: Path) -> list[Price]:
    with path.open(encoding="utf-8") as handle:
        payload = json.load(handle)
    return parse_prices(payload)


def price_index_on_or_after(days: list[date], target: date) -> int | None:
    index = bisect.bisect_left(days, target)
    return index if index < len(days) else None


def trading_day_milestone(
    stock_days: list[date],
    trading_days: list[date],
    entry_day: date,
    number: int,
) -> int | None:
    """Return the first stock quote on/after the nth TSE trading day."""

    calendar_entry = bisect.bisect_left(trading_days, entry_day)
    if calendar_entry >= len(trading_days) or trading_days[calendar_entry] != entry_day:
        raise ValueError(f"entry day is absent from trading calendar: {entry_day}")
    target_position = calendar_entry + number - 1
    if target_position >= len(trading_days):
        return None
    return price_index_on_or_after(stock_days, trading_days[target_position])


def month_end_indexes(prices: list[Price], start_index: int) -> set[int]:
    result: set[int] = set()
    for index in range(start_index, len(prices)):
        current = prices[index].day
        if index + 1 == len(prices):
            result.add(index)
            continue
        following = prices[index + 1].day
        if (current.year, current.month) != (following.year, following.month):
            result.add(index)
    return result


def moving_averages(prices: list[Price]) -> list[float | None]:
    result: list[float | None] = []
    running_sum = 0.0
    for index, price in enumerate(prices):
        running_sum += price.close
        if index >= 20:
            running_sum -= prices[index - 20].close
        result.append(running_sum / 20 if index >= 19 else None)
    return result


def simulate_episode(
    *,
    code: str,
    name: str,
    label: str,
    evaluation_day: date,
    entry_day: date,
    entry_price: float,
    prices: list[Price],
    trading_days: list[date],
) -> SimulationResult:
    """Simulate C1/C3/C4/C5/C6 and D1/D2/D4 with Q0 normalized to one."""

    stock_days = [price.day for price in prices]
    entry_index = bisect.bisect_left(stock_days, entry_day)
    if entry_index >= len(prices) or prices[entry_index].day != entry_day:
        raise ValueError(f"{code}: missing entry quote for {entry_day}")
    cached_entry = prices[entry_index].close
    if abs(cached_entry / entry_price - 1) > 1e-6:
        raise ValueError(
            f"{code}: entry mismatch CSV={entry_price} cache={cached_entry}"
        )

    month_ends = month_end_indexes(prices, entry_index)
    averages = moving_averages(prices)
    milestone_indexes = {
        rule: trading_day_milestone(stock_days, trading_days, entry_day, number)
        for rule, number in (("S-C3", 126), ("S-C4", 252), ("S-C5", 504))
    }
    milestone_rules: dict[int, list[str]] = {}
    for rule, index in milestone_indexes.items():
        if index is not None:
            milestone_rules.setdefault(index, []).append(rule)
    c6_index = price_index_on_or_after(stock_days, add_years(entry_day, 3))

    remaining = 1.0
    trades: list[Trade] = []
    pending_rule = ""
    pending_trigger_day: date | None = None
    d1_done = False
    d2_done = False
    tenx_triggered = False
    highest_close = entry_price
    highest_ma20: float | None = None

    def execute(rule: str, trigger_index: int, requested_fraction: float) -> bool:
        nonlocal remaining, pending_rule, pending_trigger_day
        execution_index = trigger_index + 1
        if execution_index >= len(prices):
            pending_rule = rule
            pending_trigger_day = prices[trigger_index].day
            return False
        fraction = min(remaining, requested_fraction)
        if fraction <= 1e-12:
            return True
        trades.append(
            Trade(
                rule=rule,
                trigger_day=prices[trigger_index].day,
                execution_day=prices[execution_index].day,
                fraction_of_q0=fraction,
                execution_price=prices[execution_index].close,
            )
        )
        remaining = max(0.0, remaining - fraction)
        return True

    for index in range(entry_index, len(prices)):
        price = prices[index]
        highest_close = max(highest_close, price.close)
        ma20 = averages[index]
        if ma20 is not None:
            highest_ma20 = ma20 if highest_ma20 is None else max(highest_ma20, ma20)

        is_month_end = index in month_ends
        if is_month_end and price.close >= 10 * entry_price:
            tenx_triggered = True

        full_exit_rule = ""
        if is_month_end and price.close <= 0.5 * entry_price:
            full_exit_rule = "S-C1"

        if not full_exit_rule:
            for rule in milestone_rules.get(index, []):
                ratio = price.close / entry_price
                if rule == "S-C3" and ratio <= 0.6:
                    full_exit_rule = rule
                    break
                if rule == "S-C4" and ratio <= 0.8 and highest_close < 1.2 * entry_price:
                    full_exit_rule = rule
                    break
                if rule == "S-C5" and ratio < 1 and highest_close < 1.5 * entry_price:
                    full_exit_rule = rule
                    break

        if (
            not full_exit_rule
            and c6_index is not None
            and index == c6_index
            and ma20 is not None
            and ma20 < 10 * entry_price
        ):
            full_exit_rule = "S-C6"

        drawdown20 = (
            ma20 / highest_ma20 - 1
            if ma20 is not None and highest_ma20 is not None
            else None
        )
        if (
            not full_exit_rule
            and is_month_end
            and tenx_triggered
            and drawdown20 is not None
            and drawdown20 <= -0.5
        ):
            full_exit_rule = "S-D4"

        if full_exit_rule:
            if execute(full_exit_rule, index, remaining):
                break
            continue

        if not is_month_end:
            continue

        if price.close >= 5 * entry_price and not d1_done:
            if execute("S-D1", index, 0.2):
                d1_done = True
        if price.close >= 10 * entry_price and not d2_done:
            if execute("S-D2", index, 0.3):
                d2_done = True

    return SimulationResult(
        code=code,
        name=name,
        label=label,
        evaluation_day=evaluation_day,
        entry_day=entry_day,
        entry_price=entry_price,
        latest_day=prices[-1].day,
        latest_price=prices[-1].close,
        trades=tuple(trades),
        pending_rule=pending_rule,
        pending_trigger_day=pending_trigger_day,
        remaining_fraction=remaining,
    )


def selected_episode(row: dict[str, str]) -> tuple[str, str]:
    if row["3y_qualified"] == "True":
        return "10x-3Y", "3y_"
    if row["2y_qualified"] == "True":
        return "10x-2Y", "2y_"
    raise ValueError(f"row is not a qualifying episode: {row['code']}")


def trade_for(result: SimulationResult, rule: str) -> Trade | None:
    return next((trade for trade in result.trades if trade.rule == rule), None)


def full_exit_trade(result: SimulationResult) -> Trade | None:
    full_rules = {"S-C1", "S-C3", "S-C4", "S-C5", "S-C6", "S-D4"}
    return next((trade for trade in result.trades if trade.rule in full_rules), None)


def date_text(value: date | None) -> str:
    return value.isoformat() if value is not None else ""


def price_text(value: float | None) -> str:
    return f"{value:.6f}".rstrip("0").rstrip(".") if value is not None else ""


def result_row(result: SimulationResult) -> dict[str, str]:
    d1 = trade_for(result, "S-D1")
    d2 = trade_for(result, "S-D2")
    full = full_exit_trade(result)
    actions = "; ".join(
        f"{trade.rule}@{trade.trigger_day.isoformat()}"
        f"->{trade.execution_day.isoformat()}:{trade.fraction_of_q0:.2f}Q0"
        f"@{price_text(trade.execution_price)}"
        for trade in result.trades
    )
    return {
        "code": result.code,
        "name": result.name,
        "label": result.label,
        "evaluation_date": result.evaluation_day.isoformat(),
        "entry_date": result.entry_day.isoformat(),
        "entry_price": price_text(result.entry_price),
        "latest_date": result.latest_day.isoformat(),
        "latest_price": price_text(result.latest_price),
        "d1_trigger_date": date_text(d1.trigger_day if d1 else None),
        "d1_execution_date": date_text(d1.execution_day if d1 else None),
        "d1_execution_price": price_text(d1.execution_price if d1 else None),
        "d2_trigger_date": date_text(d2.trigger_day if d2 else None),
        "d2_execution_date": date_text(d2.execution_day if d2 else None),
        "d2_execution_price": price_text(d2.execution_price if d2 else None),
        "full_exit_rule": full.rule if full else "",
        "full_exit_trigger_date": date_text(full.trigger_day if full else None),
        "full_exit_date": date_text(full.execution_day if full else None),
        "full_exit_price": price_text(full.execution_price if full else None),
        "remaining_fraction": f"{result.remaining_fraction:.6f}",
        "realized_cash_multiple": f"{result.realized_cash_multiple:.6f}",
        "terminal_mark_multiple": f"{result.terminal_mark_multiple:.6f}",
        "gross_value_multiple": f"{result.gross_value_multiple:.6f}",
        "gross_pnl_pct": f"{result.gross_pnl_pct:.2f}",
        "buy_hold_multiple": f"{result.buy_hold_multiple:.6f}",
        "buy_hold_pnl_pct": f"{result.buy_hold_pnl_pct:.2f}",
        "difference_vs_buy_hold_pct_points": (
            f"{result.gross_pnl_pct - result.buy_hold_pnl_pct:.2f}"
        ),
        "pending_rule": result.pending_rule,
        "pending_trigger_date": date_text(result.pending_trigger_day),
        "actions": actions,
    }


def write_csv(path: Path, results: list[SimulationResult]) -> None:
    rows = [result_row(result) for result in results]
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]), lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def yen(value: float) -> str:
    return f"¥{value:,.0f}"


def write_report(path: Path, results: list[SimulationResult], output_path: Path) -> None:
    total_cost_units = float(len(results))
    total_value_units = sum(result.gross_value_multiple for result in results)
    total_buy_hold_units = sum(result.buy_hold_multiple for result in results)
    model_pnl_pct = (total_value_units / total_cost_units - 1) * 100
    buy_hold_pnl_pct = (total_buy_hold_units / total_cost_units - 1) * 100
    model_multiples = [result.gross_value_multiple for result in results]
    buy_hold_multiples = [result.buy_hold_multiple for result in results]
    realized = sum(result.realized_cash_multiple for result in results)
    marked = sum(result.terminal_mark_multiple for result in results)
    winners = sum(result.gross_value_multiple > 1 for result in results)
    losers = sum(result.gross_value_multiple < 1 for result in results)
    flats = len(results) - winners - losers
    improved = sum(
        result.gross_value_multiple > result.buy_hold_multiple for result in results
    )
    worsened = sum(
        result.gross_value_multiple < result.buy_hold_multiple for result in results
    )

    exit_counts: dict[str, int] = {}
    for result in results:
        trade = full_exit_trade(result)
        key = trade.rule if trade else "期末保有あり"
        exit_counts[key] = exit_counts.get(key, 0) + 1

    example_per_name = 100_000
    example_cost = example_per_name * len(results)
    example_value = example_per_name * total_value_units
    example_buy_hold_value = example_per_name * total_buy_hold_units
    d4_results = [
        result
        for result in results
        if (full_exit_trade(result) and full_exit_trade(result).rule == "S-D4")
    ]
    d4_model_value = sum(result.gross_value_multiple for result in d4_results)
    d4_buy_hold_value = sum(result.buy_hold_multiple for result in d4_results)
    early_loss_results = [
        result for result in results if result.gross_value_multiple < 1
    ]
    early_loss_rule_counts: dict[str, int] = {}
    for result in early_loss_results:
        trade = full_exit_trade(result)
        key = trade.rule if trade else "不明"
        early_loss_rule_counts[key] = early_loss_rule_counts.get(key, 0) + 1
    early_loss_summary = "、".join(
        f"{rule} {count}銘柄" for rule, count in sorted(early_loss_rule_counts.items())
    )

    lines = [
        "# v0.2価格ルール適用時の参考損益（2016–2026年）",
        "",
        "- 計算日: 2026年8月31日",
        "- 株価最終日: 2026年8月28日",
        f"- 対象: 現存銘柄から事後に抽出したテンバガー {len(results)} 銘柄",
        "- 金額単位: 1銘柄へ同額の1単位を投資。税引前・配当なし・費用なし",
        "- 判定: v0.2のうち価格系列だけで一意に実行できる売却条件",
        "",
        "> [!WARNING]",
        f"> これはv0.2戦略全体のバックテストではない。対象は「後から10倍になった」と判明している現存{len(results)}銘柄だけであり、購入前スコアで選ばれた銘柄ではない。非テンバガー、上場廃止、選定漏れを含まないため、以下の合計損益を期待収益率として使えない。",
        "",
        "> [!NOTE]",
        "> 今回の約定検証で、Yahooの系列に休場日の「出来高0・前日終値据え置き」が含まれることを検出した。これを売買可能日から除いて元の価格探索も再実行したため、前版の84銘柄から83銘柄へ訂正した。7901マツモトは実際に価格が付いた日の間隔が54暦日あり、品質監査対象へ移したもので、失敗銘柄と判定したわけではない。",
        "",
        "## 結果",
        "",
        f"全{len(results)}銘柄へ同額を別々に投資した診断用集計である。銘柄ごとの投資時期が異なり、v0.2の同時保有12銘柄、候補群20%、業種6%、1銘柄1〜3%のポートフォリオ制約は再現していない。",
        "",
        "| 指標 | v0.2価格ルール | 売却せず期末保有 |",
        "|---|---:|---:|",
        f"| 投下元本 | {total_cost_units:.2f}単位 | {total_cost_units:.2f}単位 |",
        f"| 期末価値 | {total_value_units:.2f}単位 | {total_buy_hold_units:.2f}単位 |",
        f"| 損益 | {total_value_units - total_cost_units:+.2f}単位 | {total_buy_hold_units - total_cost_units:+.2f}単位 |",
        f"| 損益率 | {model_pnl_pct:+.2f}% | {buy_hold_pnl_pct:+.2f}% |",
        f"| 銘柄別倍率の中央値 | {statistics.median(model_multiples):.2f}倍 | {statistics.median(buy_hold_multiples):.2f}倍 |",
        f"| 勝ち / 負け / 同値 | {winners} / {losers} / {flats} | — |",
        "",
        f"v0.2価格ルールの期末価値は、売却済み現金 {realized:.2f}単位と未売却残高の期末評価 {marked:.2f}単位の合計である。売却せず保有した場合との差は {total_value_units - total_buy_hold_units:+.2f}単位（元本比 {model_pnl_pct - buy_hold_pnl_pct:+.2f}ポイント）だった。",
        "",
        f"金額イメージとして1銘柄10万円ずつ投じたなら、元本 {yen(example_cost)} に対してv0.2価格ルールの期末価値は {yen(example_value)}、損益は {yen(example_value - example_cost)}。売却なしは期末価値 {yen(example_buy_hold_value)}、損益 {yen(example_buy_hold_value - example_cost)}に相当する。これは倍率を円へ置き換えただけで、実際の資金拘束期間や同時保有制約を反映しない。",
        "",
        "### 結果の読み取り",
        "",
        f"- 売却なしとの銘柄別比較では、価格ルールが上回ったのは{improved}銘柄、下回ったのは{worsened}銘柄だった。中央値は価格ルールの方が高い一方、一部の長期大幅上昇を途中で縮小したため、全銘柄合計では売却なしを下回った。",
        f"- `S-D4` で全売却した{len(d4_results)}銘柄だけを見ると、価格ルールは {d4_model_value:.2f}単位、売却なしは {d4_buy_hold_value:.2f}単位だった。10倍後に崩れた銘柄では利益保全が機能した。",
        f"- 反対に、10倍になる前の価格・時間条件で損失確定した既知の成功例が{len(early_loss_results)}銘柄ある（{early_loss_summary}）。この{len(early_loss_results)}銘柄は後に全量保有ならすべてプラスになっており、価格条件だけの早期撤退には明確な機会損失がある。",
        "- したがって、この結果だけを理由に価格閾値を採用・変更できない。非成功銘柄も含め、回避できた損失と取り逃した成功を同じ母集団で比較する必要がある。",
        "",
        "### 全売却の内訳",
        "",
        "| 最終状態 / 全売却条件 | 銘柄数 |",
        "|---|---:|",
    ]
    for rule, count in sorted(exit_counts.items()):
        lines.append(f"| {rule} | {count} |")

    lines.extend(
        [
            "",
            "## 銘柄別損益",
            "",
            "`期末倍率` は、売却代金と残存株の2026年8月28日評価額を合算して初期投資額で割った値。`損益` はその倍率から1を引いた税引前損益率である。`売却なし` は同じ購入日から最終日まで全量保有した比較である。",
            "",
            "| コード | 銘柄 | ラベル | 購入日 | 売買ルール | 期末倍率 | 損益 | 売却なし | 差 |",
            "|---:|---|---|---|---|---:|---:|---:|---:|",
        ]
    )
    for result in results:
        rules = " → ".join(trade.rule for trade in result.trades) or "売却なし"
        difference = result.gross_pnl_pct - result.buy_hold_pnl_pct
        lines.append(
            f"| {result.code} | {result.name} | {result.label} | "
            f"{result.entry_day.isoformat()} | {rules} | "
            f"{result.gross_value_multiple:.2f}倍 | {result.gross_pnl_pct:+.2f}% | "
            f"{result.buy_hold_multiple:.2f}倍 | {difference:+.2f}pt |"
        )

    output_link = f"../{output_path.as_posix()}"
    lines.extend(
        [
            "",
            f"約定日、約定価格、売却比率、現金と残高の内訳は[銘柄別CSV]({output_link})に収録した。",
            "",
            "## 適用した条件と適用できない条件",
            "",
            "| 区分 | 今回の扱い |",
            "|---|---|",
            "| 購入判定・追加購入 | 未適用。過去の100点スコア、ハードゲート、日中高安値、当時の発行株式数がない |",
            "| `S-C1`、`S-C3`、`S-C6` | 適用 |",
            "| `S-C4` | `購入後最高終値 < 1.2 × PA` の価格だけで確定する分岐を適用。`S < 70` 分岐は未適用 |",
            "| `S-C5` | `購入後最高終値 < 1.5 × PA` の価格だけで確定する分岐を適用。`S < 75` 分岐は未適用 |",
            "| `S-D1`、`S-D2`、`S-D4` | 適用 |",
            "| `S-A1`〜`S-A6`、`S-B1`〜`S-B5` | 未適用。point-in-timeの開示、財務、監査、希薄化、SAM/SOMがない |",
            "| `S-C2`、`S-D3`、`S-D5` | 未適用。スコアまたは主要KPIがない |",
            "| `S-D6` | 未適用。時系列ポートフォリオを構成していない |",
            "",
            "## 計算方法",
            "",
            "1. 各銘柄は最初の `10x-3Y` エピソードを使い、3年該当がない1銘柄だけ `10x-2Y` を使った。評価日翌日の終値を `PA`、初期株数を `Q0 = 1` とした。",
            "2. 出来高0の休場日プレースホルダーを除き、126・252・504営業日は7203（トヨタ自動車）の取引日から復元した東証営業日カレンダーで数えた。銘柄の売買停止日は営業日数から除いていない。",
            "3. 月末条件は各銘柄の月内最終価格で判定した。条件成立日の価格では売らず、その銘柄の次の価格記録日の終値で約定した。",
            "4. `S-D1` は `0.20 × Q0`、`S-D2` は追加の `0.30 × Q0` を売却した。全売却条件は部分売却より優先し、残量を売却した。",
            "5. 最終日まで残った株数は最終終値で評価した。売却代金は現金のまま置き、再投資、配当、金利、税、手数料、スプレッド、スリッページを含めていない。",
            "",
            "## この結果から言えること / 言えないこと",
            "",
            "今回測れるのは、既知の勝者に対して価格売却ルールがどれだけ利益を残したか、または売却なしに比べて上値を失ったかである。価格ルールの実装確認と、成功例を早く手放す機会損失の診断には使える。",
            "",
            "v0.2の銘柄選択能力、負け銘柄の損失抑制、実際のポートフォリオ損益、最大ドローダウン、再現率・適合率は測れない。これらには、各評価月の全上場銘柄（上場廃止を含む）と、その時点までに公表された財務・開示・完全希薄化後株式数・SAM/SOM・競合データが必要である。",
            "",
            "## データ",
            "",
            "- 対象一覧: [現存テンバガー価格エピソード](../data/tenbagger-survivor-price-episodes-2016-2026.csv)",
            f"- 計算結果: [銘柄別損益CSV]({output_link})",
            "- 元の検証: [日本株テンバガー仮説の予備検証](tenbagger-validation-2016-2026.md)",
            "- 運用仕様: [日本株テンバガー判定・運用ルール v0.2](tenbagger-rule-v0.2.md)",
            "",
            "生のYahoo Finance価格キャッシュはリポジトリに含めていない。候補探索用の二次データであり、最終的な投資検証ではJPXの調整株価とコーポレートアクションで再照合する。",
        ]
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def load_results(
    episodes_path: Path, cache_dir: Path, calendar_code: str
) -> list[SimulationResult]:
    calendar_prices = load_prices(cache_dir / f"{calendar_code}.json")
    trading_days = [price.day for price in calendar_prices]
    with episodes_path.open(encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))

    results: list[SimulationResult] = []
    for row in rows:
        label, prefix = selected_episode(row)
        prices = load_prices(cache_dir / f"{row['code']}.json")
        results.append(
            simulate_episode(
                code=row["code"],
                name=row["name"],
                label=label,
                evaluation_day=date.fromisoformat(row[prefix + "evaluation_date"]),
                entry_day=date.fromisoformat(row[prefix + "entry_date"]),
                entry_price=float(row[prefix + "entry_close"]),
                prices=prices,
                trading_days=trading_days,
            )
        )
    return results


def main() -> int:
    args = parse_args()
    results = load_results(args.episodes, args.cache_dir, args.calendar_code)
    write_csv(args.output, results)
    write_report(args.report, results, args.output)
    total_value = sum(result.gross_value_multiple for result in results)
    print(
        f"episodes={len(results)} cost={len(results):.2f} "
        f"value={total_value:.6f} pnl={total_value - len(results):+.6f} "
        f"output={args.output} report={args.report}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
