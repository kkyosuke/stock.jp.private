#!/usr/bin/env python3
"""Replay v0.2 versus v0.4 allocation over the complete 2025 price archive.

This is deliberately an allocation diagnostic, not a full tenbagger strategy
backtest and not a substitute for explicit LIVE promotion review.
Point-in-time fundamental, disclosure, dilution, SAM/SOM and MRS inputs are not
available. Candidates therefore come from a frozen price/liquidity proxy screen,
and every scheduled add is an explicit all-gates-pass stress assumption.
"""

from __future__ import annotations

import argparse
import csv
from collections import defaultdict
from dataclasses import dataclass
from datetime import date
import json
import math
from pathlib import Path
from typing import Iterable

try:
    from scripts.position_sizing import AllocationCaps, allocation_caps
except ModuleNotFoundError:  # Direct execution: python scripts/<file>.py
    from position_sizing import AllocationCaps, allocation_caps


DEFAULT_CANDIDATES = Path("data/tenbagger-v0.4-allocation-replay-2025-candidates.csv")
DEFAULT_TRADES = Path("data/tenbagger-v0.4-allocation-replay-2025-trades.csv")
DEFAULT_DAILY = Path("data/tenbagger-v0.4-allocation-replay-2025-daily.csv")
DEFAULT_SUMMARY = Path("data/tenbagger-v0.4-allocation-replay-2025-summary.json")
DEFAULT_REPORT = Path("docs/research/tenbagger-v0.4-allocation-replay-2025.md")
BOARD_LOT = 100
FEE_RATE = 0.0015
EXCLUDED_PROXY_SECTORS = {
    "医薬品",
    "銀行業",
    "証券･商品先物取引業",
    "保険業",
    "その他金融業",
}


@dataclass(frozen=True)
class Bar:
    day: date
    open: float
    high: float
    low: float
    close: float
    volume: int


@dataclass(frozen=True)
class Candidate:
    rank: int
    code: str
    name: str
    sector: str
    market: str
    selection_day: date
    selection_close: float
    momentum: float
    average_turnover_20d: float
    observations: int


@dataclass(frozen=True)
class Fill:
    rule_version: str
    tranche: str
    evaluation_day: date
    trade_day: date
    code: str
    name: str
    sector: str
    price: float
    quantity: int
    gross_cost: float
    fee: float


@dataclass(frozen=True)
class SkippedOrder:
    rule_version: str
    tranche: str
    trade_day: date
    code: str
    reason: str


@dataclass(frozen=True)
class PortfolioRun:
    rule_version: str
    fills: tuple[Fill, ...]
    skipped: tuple[SkippedOrder, ...]
    daily_rows: tuple[dict[str, float | str], ...]
    summary: dict[str, float | int | str]


def _number(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed) or parsed <= 0:
        raise ValueError(f"price must be finite and positive: {value!r}")
    return parsed


def _read_bars(
    path: Path, wanted: set[str] | None = None
) -> list[tuple[str, str, str, str, Bar]]:
    result: list[tuple[str, str, str, str, Bar]] = []
    with path.open(encoding="utf-8", newline="") as source:
        for row in csv.DictReader(source):
            code = row["銘柄コード"].strip()
            if wanted is not None and code not in wanted:
                continue
            if row["取得状態"] != "OK" or "内国株式" not in row["市場・商品区分"]:
                continue
            if not all(row[field].strip() for field in ("始値", "高値", "安値", "終値", "売買高(株)")):
                continue
            result.append(
                (
                    code,
                    row["銘柄名"].strip(),
                    row["33業種区分"].strip(),
                    row["市場・商品区分"].strip(),
                    Bar(
                        day=date.fromisoformat(row["日付"]),
                        open=_number(row["始値"]),
                        high=_number(row["高値"]),
                        low=_number(row["安値"]),
                        close=_number(row["終値"]),
                        volume=int(row["売買高(株)"]),
                    ),
                )
            )
    return result


def select_candidates(
    *,
    archive: Path,
    selection_day: date,
    initial_capital: float,
    count: int = 12,
    lookback: int = 120,
    minimum_momentum: float = 0.20,
    maximum_momentum: float = 3.00,
    minimum_turnover: float = 100_000_000,
    sector_cap: int = 2,
) -> tuple[list[Candidate], int]:
    history: dict[str, list[Bar]] = defaultdict(list)
    metadata: dict[str, tuple[str, str, str]] = {}
    files = [
        path
        for path in sorted(archive.glob("*/*.csv"))
        if date.fromisoformat(path.stem) <= selection_day
    ]
    for path in files:
        for code, name, sector, market, bar in _read_bars(path):
            history[code].append(bar)
            metadata[code] = (name, sector, market)

    # A 100-share lot must fit the stricter v0.2 1% initial cap so both
    # portfolios receive the same candidates and execution opportunities.
    maximum_close = initial_capital * allocation_caps("v0.2").initial_entry_pct / 100 / BOARD_LOT
    ranked: list[tuple[float, float, str, str, str, str, Bar, int]] = []
    for code, observations in history.items():
        observations.sort(key=lambda item: item.day)
        if len(observations) < lookback or observations[-1].day != selection_day:
            continue
        name, sector, market = metadata[code]
        if sector in EXCLUDED_PROXY_SECTORS:
            continue
        window = observations[-lookback:]
        latest = window[-1]
        momentum = latest.close / window[0].close - 1
        turnover = sum(bar.close * bar.volume for bar in observations[-20:]) / 20
        if latest.close > maximum_close:
            continue
        if not minimum_momentum <= momentum <= maximum_momentum:
            continue
        if turnover < minimum_turnover:
            continue
        ranked.append(
            (momentum, turnover, code, name, sector, market, latest, len(observations))
        )
    ranked.sort(key=lambda item: (-item[0], -item[1], item[2]))

    selected: list[Candidate] = []
    sector_counts: dict[str, int] = defaultdict(int)
    for momentum, turnover, code, name, sector, market, latest, observations in ranked:
        if sector_counts[sector] >= sector_cap:
            continue
        selected.append(
            Candidate(
                rank=len(selected) + 1,
                code=code,
                name=name,
                sector=sector,
                market=market,
                selection_day=selection_day,
                selection_close=latest.close,
                momentum=momentum,
                average_turnover_20d=turnover,
                observations=observations,
            )
        )
        sector_counts[sector] += 1
        if len(selected) == count:
            break
    if len(selected) != count:
        raise ValueError(f"proxy screen produced {len(selected)} candidates, expected {count}")
    return selected, len(ranked)


def load_replay_bars(
    *, archive: Path, candidates: list[Candidate], start: date, end: date
) -> tuple[list[date], dict[str, dict[date, Bar]]]:
    wanted = {candidate.code for candidate in candidates}
    bars: dict[str, dict[date, Bar]] = defaultdict(dict)
    sessions: list[date] = []
    for path in sorted(archive.glob("*/*.csv")):
        day = date.fromisoformat(path.stem)
        if day < start or day > end:
            continue
        sessions.append(day)
        for code, _, _, _, bar in _read_bars(path, wanted):
            bars[code][day] = bar
    sessions = sorted(set(sessions))
    if not sessions or sessions[0] != start or sessions[-1] != end:
        raise ValueError("replay archive does not cover the requested endpoints")
    for candidate in candidates:
        missing = [day for day in sessions if day not in bars[candidate.code]]
        if missing:
            raise ValueError(
                f"{candidate.code} is missing {len(missing)} replay sessions; "
                "do not silently carry prices forward"
            )
        ordered = [bars[candidate.code][day].close for day in sessions]
        suspicious = [
            current / previous
            for previous, current in zip(ordered, ordered[1:])
            if current / previous <= 0.5 or current / previous >= 2.0
        ]
        if suspicious:
            raise ValueError(
                f"{candidate.code} has a possible unadjusted corporate action; "
                "abort instead of selectively excluding an outcome"
            )
    return sessions, bars


def _first_session_on_or_after(sessions: list[date], requested: date) -> date:
    return next(day for day in sessions if day >= requested)


def _previous_session(sessions: list[date], trade_day: date) -> date:
    index = sessions.index(trade_day)
    if index == 0:
        raise ValueError("trade day has no prior evaluation session")
    return sessions[index - 1]


def _model_fill_price(evaluation_close: float, trade_bar: Bar) -> float | None:
    limit = 1.15 * evaluation_close
    if trade_bar.open <= limit:
        return trade_bar.open
    if trade_bar.low <= limit:
        return limit
    return None


def build_fills(
    *,
    rule_version: str,
    candidates: list[Candidate],
    sessions: list[date],
    bars: dict[str, dict[date, Bar]],
    initial_capital: float,
    scheduled_tranches: tuple[tuple[str, date], ...],
    fee_rate: float = FEE_RATE,
) -> tuple[list[Fill], list[SkippedOrder]]:
    caps = allocation_caps(rule_version)
    cash = initial_capital
    total_cost = 0.0
    name_cost: dict[str, float] = defaultdict(float)
    industry_cost: dict[str, float] = defaultdict(float)
    fills: list[Fill] = []
    skipped: list[SkippedOrder] = []

    for tranche, requested_day in scheduled_tranches:
        trade_day = _first_session_on_or_after(sessions, requested_day)
        first_replay_session = trade_day == sessions[0]
        evaluation_day = (
            candidates[0].selection_day
            if first_replay_session
            else _previous_session(sessions, trade_day)
        )
        tranche_pct = (
            caps.initial_entry_pct if tranche == "INITIAL" else caps.add_entry_pct
        )
        for candidate in candidates:
            evaluation_close = (
                candidate.selection_close
                if first_replay_session
                else bars[candidate.code][evaluation_day].close
            )
            price = _model_fill_price(
                evaluation_close,
                bars[candidate.code][trade_day],
            )
            if price is None:
                skipped.append(
                    SkippedOrder(rule_version, tranche, trade_day, candidate.code, "LIMIT_NOT_FILLED")
                )
                continue
            ceilings = (
                initial_capital * tranche_pct / 100,
                initial_capital * caps.single_name_cost_pct / 100 - name_cost[candidate.code],
                initial_capital * caps.candidate_pool_cost_pct / 100 - total_cost,
                initial_capital * caps.industry_cost_pct / 100 - industry_cost[candidate.sector],
                cash / (1 + fee_rate),
            )
            available = max(0.0, min(ceilings))
            quantity = math.floor(available / price / BOARD_LOT) * BOARD_LOT
            if quantity <= 0:
                skipped.append(
                    SkippedOrder(rule_version, tranche, trade_day, candidate.code, "CAP_OR_BOARD_LOT")
                )
                continue
            gross_cost = price * quantity
            fee = gross_cost * fee_rate
            cash -= gross_cost + fee
            total_cost += gross_cost
            name_cost[candidate.code] += gross_cost
            industry_cost[candidate.sector] += gross_cost
            fills.append(
                Fill(
                    rule_version=rule_version,
                    tranche=tranche,
                    evaluation_day=evaluation_day,
                    trade_day=trade_day,
                    code=candidate.code,
                    name=candidate.name,
                    sector=candidate.sector,
                    price=price,
                    quantity=quantity,
                    gross_cost=gross_cost,
                    fee=fee,
                )
            )
    return fills, skipped


def value_portfolio(
    *,
    rule_version: str,
    candidates: list[Candidate],
    sessions: list[date],
    bars: dict[str, dict[date, Bar]],
    initial_capital: float,
    fills: list[Fill],
    skipped: list[SkippedOrder],
) -> PortfolioRun:
    fills_by_day: dict[date, list[Fill]] = defaultdict(list)
    for fill in fills:
        fills_by_day[fill.trade_day].append(fill)
    cash = initial_capital
    quantities: dict[str, int] = defaultdict(int)
    acquisition_cost = 0.0
    fees = 0.0
    peak_nav = initial_capital
    maximum_drawdown = 0.0
    daily_rows: list[dict[str, float | str]] = []
    for day in sessions:
        for fill in fills_by_day[day]:
            cash -= fill.gross_cost + fill.fee
            quantities[fill.code] += fill.quantity
            acquisition_cost += fill.gross_cost
            fees += fill.fee
        market_value = sum(
            quantities[candidate.code] * bars[candidate.code][day].close
            for candidate in candidates
        )
        nav = cash + market_value
        peak_nav = max(peak_nav, nav)
        drawdown = nav / peak_nav - 1
        maximum_drawdown = min(maximum_drawdown, drawdown)
        daily_rows.append(
            {
                "date": day.isoformat(),
                "cash": cash,
                "market_value": market_value,
                "nav": nav,
                "drawdown_pct": drawdown * 100,
                "acquisition_cost_pct": acquisition_cost / initial_capital * 100,
                "strategy_waiting_cash_pct": cash / nav * 100,
            }
        )
    final = daily_rows[-1]
    summary: dict[str, float | int | str] = {
        "rule_version": rule_version,
        "initial_capital": initial_capital,
        "final_nav": round(float(final["nav"]), 2),
        "return_pct": round((float(final["nav"]) / initial_capital - 1) * 100, 6),
        "maximum_drawdown_pct": round(maximum_drawdown * 100, 6),
        "final_cash": round(float(final["cash"]), 2),
        "final_market_value": round(float(final["market_value"]), 2),
        "final_strategy_waiting_cash_pct": round(
            float(final["strategy_waiting_cash_pct"]), 6
        ),
        "acquisition_cost_pct": round(acquisition_cost / initial_capital * 100, 6),
        "fees": round(fees, 2),
        "fill_count": len(fills),
        "skipped_order_count": len(skipped),
    }
    return PortfolioRun(rule_version, tuple(fills), tuple(skipped), tuple(daily_rows), summary)


def run_replay(
    *,
    archive: Path,
    initial_capital: float,
    selection_day: date,
    start: date,
    end: date,
) -> tuple[list[Candidate], int, list[date], dict[str, dict[date, Bar]], list[PortfolioRun]]:
    candidates, eligible_count = select_candidates(
        archive=archive,
        selection_day=selection_day,
        initial_capital=initial_capital,
    )
    sessions, bars = load_replay_bars(
        archive=archive, candidates=candidates, start=start, end=end
    )
    schedule = (
        ("INITIAL", start),
        ("ADD_1", date(start.year, 4, 1)),
        ("ADD_2", date(start.year, 7, 1)),
    )
    runs: list[PortfolioRun] = []
    for version in ("v0.2", "v0.4"):
        fills, skipped = build_fills(
            rule_version=version,
            candidates=candidates,
            sessions=sessions,
            bars=bars,
            initial_capital=initial_capital,
            scheduled_tranches=schedule,
        )
        runs.append(
            value_portfolio(
                rule_version=version,
                candidates=candidates,
                sessions=sessions,
                bars=bars,
                initial_capital=initial_capital,
                fills=fills,
                skipped=skipped,
            )
        )
    return candidates, eligible_count, sessions, bars, runs


def _write_csv(path: Path, fields: list[str], rows: Iterable[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as destination:
        writer = csv.DictWriter(destination, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def write_outputs(
    *,
    candidates: list[Candidate],
    eligible_count: int,
    sessions: list[date],
    runs: list[PortfolioRun],
    initial_capital: float,
    selection_day: date,
    candidates_path: Path,
    trades_path: Path,
    daily_path: Path,
    summary_path: Path,
    report_path: Path,
) -> None:
    _write_csv(
        candidates_path,
        [
            "rank", "code", "name", "sector", "market", "selection_date",
            "selection_close", "momentum_120d_pct", "average_turnover_20d",
            "observations",
        ],
        (
            {
                "rank": item.rank,
                "code": item.code,
                "name": item.name,
                "sector": item.sector,
                "market": item.market,
                "selection_date": item.selection_day.isoformat(),
                "selection_close": f"{item.selection_close:.6f}".rstrip("0").rstrip("."),
                "momentum_120d_pct": f"{item.momentum * 100:.6f}",
                "average_turnover_20d": f"{item.average_turnover_20d:.2f}",
                "observations": item.observations,
            }
            for item in candidates
        ),
    )
    _write_csv(
        trades_path,
        [
            "rule_version", "tranche", "evaluation_date", "trade_date", "code",
            "name", "sector", "fill_price", "quantity", "gross_cost", "fee",
            "gross_cost_pct",
        ],
        (
            {
                "rule_version": fill.rule_version,
                "tranche": fill.tranche,
                "evaluation_date": fill.evaluation_day.isoformat(),
                "trade_date": fill.trade_day.isoformat(),
                "code": fill.code,
                "name": fill.name,
                "sector": fill.sector,
                "fill_price": f"{fill.price:.6f}",
                "quantity": fill.quantity,
                "gross_cost": f"{fill.gross_cost:.2f}",
                "fee": f"{fill.fee:.2f}",
                "gross_cost_pct": f"{fill.gross_cost / initial_capital * 100:.6f}",
            }
            for run in runs
            for fill in run.fills
        ),
    )
    daily_by_version = {run.rule_version: run.daily_rows for run in runs}
    _write_csv(
        daily_path,
        [
            "date", "v02_nav", "v02_cash", "v02_market_value", "v02_drawdown_pct",
            "v02_acquisition_cost_pct", "v02_strategy_waiting_cash_pct", "v04_nav",
            "v04_cash", "v04_market_value", "v04_drawdown_pct",
            "v04_acquisition_cost_pct", "v04_strategy_waiting_cash_pct",
        ],
        (
            {
                "date": left["date"],
                "v02_nav": f"{float(left['nav']):.2f}",
                "v02_cash": f"{float(left['cash']):.2f}",
                "v02_market_value": f"{float(left['market_value']):.2f}",
                "v02_drawdown_pct": f"{float(left['drawdown_pct']):.6f}",
                "v02_acquisition_cost_pct": f"{float(left['acquisition_cost_pct']):.6f}",
                "v02_strategy_waiting_cash_pct": f"{float(left['strategy_waiting_cash_pct']):.6f}",
                "v04_nav": f"{float(right['nav']):.2f}",
                "v04_cash": f"{float(right['cash']):.2f}",
                "v04_market_value": f"{float(right['market_value']):.2f}",
                "v04_drawdown_pct": f"{float(right['drawdown_pct']):.6f}",
                "v04_acquisition_cost_pct": f"{float(right['acquisition_cost_pct']):.6f}",
                "v04_strategy_waiting_cash_pct": f"{float(right['strategy_waiting_cash_pct']):.6f}",
            }
            for left, right in zip(
                daily_by_version["v0.2"], daily_by_version["v0.4"], strict=True
            )
        ),
    )
    summaries = {run.rule_version: run.summary for run in runs}
    payload = {
        "schema_version": 1,
        "status": "ALLOCATION_DIAGNOSTIC_ONLY",
        "selection_date": selection_day.isoformat(),
        "replay_period": {"from": sessions[0].isoformat(), "through": sessions[-1].isoformat()},
        "session_count": len(sessions),
        "initial_capital": initial_capital,
        "candidate_count": len(candidates),
        "eligible_proxy_count": eligible_count,
        "transaction_cost_rate": FEE_RATE,
        "mrs_assumption": "NORMAL",
        "add_assumption": "ALL_PROXY_CANDIDATES_PASS_UNOBSERVED_QUARTERLY_ADD_GATES",
        "full_strategy_backtest": False,
        "forward_paper_gate_satisfied": False,
        "results": summaries,
    }
    summary_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    v02 = summaries["v0.2"]
    v04 = summaries["v0.4"]
    lines = [
        "# v0.4資金配分の12か月過去データ実験（2025年）",
        "",
        "- 状態: `ALLOCATION_DIAGNOSTIC_ONLY`",
        f"- 候補選定日: {selection_day.isoformat()}",
        f"- 再生期間: {sessions[0].isoformat()}〜{sessions[-1].isoformat()}（{len(sessions)}営業日）",
        f"- 比較用初期資産: ¥{initial_capital:,.0f}（個人資産額ではない）",
        "- 比較: v0.2資金配分 対 v0.4全額テンバガー資金配分",
        "",
        "> これは12か月の前向きPAPER運用ではなく、そのLIVEゲートを満たさない。財務・開示・希薄化・SAM/SOM・MRSのpoint-in-time入力が不足するため、銘柄選定能力と完全な入口・出口は検証していない。結果は資金配分による損益とドローダウンの増幅を診断するものに限る。",
        "",
        "## 結果",
        "",
        "| 指標 | v0.2 | v0.4 |",
        "|---|---:|---:|",
        f"| 取得原価ベース投資率 | {float(v02['acquisition_cost_pct']):.2f}% | {float(v04['acquisition_cost_pct']):.2f}% |",
        f"| 期末資産 | ¥{float(v02['final_nav']):,.0f} | ¥{float(v04['final_nav']):,.0f} |",
        f"| 12か月損益率 | {float(v02['return_pct']):+.2f}% | {float(v04['return_pct']):+.2f}% |",
        f"| 最大ドローダウン | {float(v02['maximum_drawdown_pct']):.2f}% | {float(v04['maximum_drawdown_pct']):.2f}% |",
        f"| 期末戦略待機資金比率 | {float(v02['final_strategy_waiting_cash_pct']):.2f}% | {float(v04['final_strategy_waiting_cash_pct']):.2f}% |",
        f"| 売買費用 | ¥{float(v02['fees']):,.0f} | ¥{float(v04['fees']):,.0f} |",
        f"| 約定トランシェ数 | {int(v02['fill_count'])} | {int(v04['fill_count'])} |",
        "",
        f"v0.4の期末資産差はv0.2比 **¥{float(v04['final_nav']) - float(v02['final_nav']):+,.0f}**、最大ドローダウン差は **{float(v04['maximum_drawdown_pct']) - float(v02['maximum_drawdown_pct']):+.2f}ポイント** だった。これは候補の良否ではなく、同じ値動きへ大きな資金を配分した影響である。",
        "",
        "## 固定した代理候補契約",
        "",
        f"2024年末までの情報だけを使い、代理条件に合格した{eligible_count}銘柄から12銘柄を順位固定した。",
        "",
        "1. 2024年12月30日に有効な国内株日足があり、直近120観測以上",
        "2. 120観測モメンタム20%以上300%以下",
        "3. 直近20日平均売買代金1億円以上",
        "4. v0.2の初回1%でも100株を購入できる終値1,000円以下",
        "5. 金融4業種と医薬品を代理画面から除外",
        "6. 同一業種最大2銘柄、モメンタム、流動性、コード順で上位12銘柄",
        "",
        "この代理条件はv0.2/v0.4の70点スコアではない。公式履歴日足のpoint-in-time日次母集団を使い、将来リターンを使って候補を選んでいないが、価格モメンタム銘柄に限定した別戦略のサンプルである。",
        "",
        "## 執行契約",
        "",
        "- 初回は2025年最初の営業日、追加は4月1日以後と7月1日以後の最初の営業日",
        "- 前営業日終値の1.15倍を指値上限とし、寄付または日中安値で約定可能な場合だけ購入",
        "- 100株単位へ切り下げ、候補群・銘柄・業種・現金上限を適用",
        "- MRSは`NORMAL`、全代理候補が未観測の四半期追加条件を満たすというストレス仮定",
        "- 売買ごとに約定代金の0.15%を費用控除",
        "- 配当、税、貸株、金利は含めない",
        "- 入口・出口の財務条件がないため、12月30日まで保有する配分比較",
        "",
        "## 解釈と限界",
        "",
        "- v0.4の損益も最大DDも、候補が同じなら概ね投資率に応じて増幅する。",
        "- 100株単位、指値、費用、候補群上限により、候補群100%でも投資率が100%へ一致するとは限らない。残りは安全資産ではなく戦略待機資金である。",
        "- 未観測の追加条件を全通過としたため、実運用より追加が多い可能性が高い。",
        "- 未調整表示価格を使用している。選定12銘柄の隣接終値倍率が0.5倍以下または2倍以上なら実験全体を停止するが、公式コーポレートアクション表による完全調整ではない。",
        "- 正式な検証にはpoint-in-time財務・開示、全ハードゲート、100点スコア、MRS、上場廃止、税・費用、全出口条件が必要である。",
        "- PAPERの経過日数と連続run数はLIVEリリース条件にしない。",
        "",
        "## 再現",
        "",
        "```bash",
        ".venv/bin/python scripts/tenbagger_v04_allocation_replay.py",
        "```",
        "",
        f"候補は[{candidates_path.name}](../data/{candidates_path.name})、約定は[{trades_path.name}](../data/{trades_path.name})、日次資産は[{daily_path.name}](../data/{daily_path.name})、機械可読集計は[{summary_path.name}](../data/{summary_path.name})に保存する。",
        "",
        "本実験は将来の利益または損失範囲を予測・保証しない。",
    ]
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, required=True, help="explicit authorized archive; no public fallback")
    parser.add_argument("--initial-capital", type=float, default=10_000_000)
    parser.add_argument("--selection-date", type=date.fromisoformat, default=date(2024, 12, 30))
    parser.add_argument("--start", type=date.fromisoformat, default=date(2025, 1, 6))
    parser.add_argument("--end", type=date.fromisoformat, default=date(2025, 12, 30))
    parser.add_argument("--candidates", type=Path, default=DEFAULT_CANDIDATES)
    parser.add_argument("--trades", type=Path, default=DEFAULT_TRADES)
    parser.add_argument("--daily", type=Path, default=DEFAULT_DAILY)
    parser.add_argument("--summary", type=Path, default=DEFAULT_SUMMARY)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if not math.isfinite(args.initial_capital) or args.initial_capital <= 0:
        raise ValueError("initial-capital must be finite and positive")
    candidates, eligible_count, sessions, _, runs = run_replay(
        archive=args.archive,
        initial_capital=args.initial_capital,
        selection_day=args.selection_date,
        start=args.start,
        end=args.end,
    )
    write_outputs(
        candidates=candidates,
        eligible_count=eligible_count,
        sessions=sessions,
        runs=runs,
        initial_capital=args.initial_capital,
        selection_day=args.selection_date,
        candidates_path=args.candidates,
        trades_path=args.trades,
        daily_path=args.daily,
        summary_path=args.summary,
        report_path=args.report,
    )
    print(json.dumps({run.rule_version: run.summary for run in runs}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
