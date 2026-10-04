"""Plot saved results; needs pip install -e '.[research]'."""
from pathlib import Path
import os

ROOT = Path(__file__).resolve().parents[2]
RESULT = Path(__file__).resolve().parent
os.environ["MPLCONFIGDIR"] = str(ROOT / ".cache/matplotlib")

import csv
from datetime import date
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates

matplotlib.rcParams["svg.hashsalt"] = "allocation-validation-2026-10-05"
with (RESULT / "continuous-daily.csv").open() as stream:
    rows = list(csv.DictReader(stream))
with (RESULT / "benchmark.csv").open() as stream:
    benchmark = list(csv.DictReader(stream))
days = [date(2024, 12, 30), *[date.fromisoformat(row["date"]) for row in rows]]
series = {
    "v0.2 allocation": [100, *[float(row["v02_nav"]) / 100000 for row in rows]],
    "v0.4 allocation": [100, *[float(row["v04_nav"]) / 100000 for row in rows]],
    "TOPIX price index": [float(row["topix_price_index"]) /
                          float(benchmark[0]["topix_price_index"]) * 100 for row in benchmark],
}
colors = ["#2c7fb8", "#d95f02", "#667571"]
figure, axes = plt.subplots(2, 1, figsize=(10, 6.8), sharex=True,
                            gridspec_kw={"height_ratios": [2, 1]}, layout="constrained")
for (label, values), color in zip(series.items(), colors):
    axes[0].plot(days, values, label=label, color=color, linewidth=1.5)
    peak = 100
    drawdowns = []
    for value in values:
        peak = max(peak, value)
        drawdowns.append((value / peak - 1) * 100)
    axes[1].plot(days, drawdowns, color=color, linewidth=1.3)
axes[0].set_title("2025–2026 allocation diagnostic: frozen price / liquidity cohort", loc="left")
axes[0].set_ylabel("Value (initial = 100)")
axes[0].legend(loc="upper left", frameon=False)
axes[1].set_ylabel("Drawdown (%)")
axes[1].xaxis.set_major_locator(mdates.MonthLocator(interval=3))
axes[1].xaxis.set_major_formatter(mdates.DateFormatter("%Y-%m"))
for axis in axes:
    axis.grid(axis="y", color="#dddddd", linewidth=0.6)
    axis.spines[["top", "right"]].set_visible(False)
figure.supxlabel("12 proxy candidates; 0.15% per-buy cost; split-adjusted shares.\n"
                  "MRS and adds assumed to pass; no fundamental exits. TOPIX excludes costs/dividends.", fontsize=9)
figure.savefig(RESULT / "allocation-chart.svg", metadata={"Date": None})
svg = RESULT / "allocation-chart.svg"
svg.write_text("\n".join(line.rstrip() for line in svg.read_text().splitlines()) + "\n")
figure.savefig(RESULT / "allocation-chart.png", dpi=170)
