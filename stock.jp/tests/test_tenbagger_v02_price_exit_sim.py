from datetime import date, timedelta
import unittest

from scripts.tenbagger_price_scan import Price
from scripts.tenbagger_v02_price_exit_sim import simulate_episode


def weekdays(start: date, end: date) -> list[date]:
    days: list[date] = []
    current = start
    while current <= end:
        if current.weekday() < 5:
            days.append(current)
        current += timedelta(days=1)
    return days


def make_prices(days: list[date], close_for_day) -> list[Price]:
    return [Price(day, float(close_for_day(day)), 100) for day in days]


class TenbaggerV02PriceExitSimTest(unittest.TestCase):
    def test_d1_d2_and_three_year_exit_use_next_closes(self) -> None:
        days = weekdays(date(2020, 1, 2), date(2023, 1, 6))

        def close(day: date) -> float:
            if day < date(2020, 3, 1):
                return 10
            if day < date(2020, 6, 1):
                return 60
            if day < date(2020, 8, 1):
                return 110
            return 90

        result = simulate_episode(
            code="0001",
            name="test",
            label="10x-3Y",
            evaluation_day=date(2020, 1, 1),
            entry_day=days[0],
            entry_price=10,
            prices=make_prices(days, close),
            trading_days=days,
        )

        self.assertEqual(
            [trade.rule for trade in result.trades],
            ["S-D1", "S-D2", "S-C6"],
        )
        self.assertEqual(
            [trade.fraction_of_q0 for trade in result.trades],
            [0.2, 0.3, 0.5],
        )
        self.assertAlmostEqual(result.gross_value_multiple, 9.0)
        self.assertEqual(result.remaining_fraction, 0)
        for trade in result.trades:
            self.assertGreater(trade.execution_day, trade.trigger_day)

    def test_c3_exits_at_the_next_close(self) -> None:
        days = weekdays(date(2020, 1, 2), date(2020, 8, 1))
        target = days[125]
        following = days[126]
        prices = make_prices(
            days,
            lambda day: 5.5 if day == target else (5.4 if day == following else 10),
        )

        result = simulate_episode(
            code="0002",
            name="test",
            label="10x-3Y",
            evaluation_day=date(2020, 1, 1),
            entry_day=days[0],
            entry_price=10,
            prices=prices,
            trading_days=days,
        )

        self.assertEqual(len(result.trades), 1)
        self.assertEqual(result.trades[0].rule, "S-C3")
        self.assertEqual(result.trades[0].trigger_day, target)
        self.assertEqual(result.trades[0].execution_day, following)
        self.assertAlmostEqual(result.gross_value_multiple, 0.54)

    def test_d4_sells_the_remaining_half_after_a_fifty_percent_ma_drawdown(self) -> None:
        days = weekdays(date(2020, 1, 2), date(2021, 1, 10))

        def close(day: date) -> float:
            if day < date(2020, 4, 1):
                return 10
            if day < date(2020, 9, 1):
                return 120
            return 50

        result = simulate_episode(
            code="0003",
            name="test",
            label="10x-3Y",
            evaluation_day=date(2020, 1, 1),
            entry_day=days[0],
            entry_price=10,
            prices=make_prices(days, close),
            trading_days=days,
        )

        self.assertEqual(
            [trade.rule for trade in result.trades],
            ["S-D1", "S-D2", "S-D4"],
        )
        self.assertEqual(result.remaining_fraction, 0)
        self.assertAlmostEqual(result.gross_value_multiple, 8.5)


if __name__ == "__main__":
    unittest.main()
