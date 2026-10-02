from datetime import date, timedelta
import unittest

from scripts.tenbagger_price_scan import Price
from scripts.tenbagger_v03_price_rules import simulate_v03_price_rules


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


class TenbaggerV03PriceRulesTest(unittest.TestCase):
    def test_drop_only_reviews_and_five_x_then_ten_x_take_profits(self) -> None:
        days = weekdays(date(2020, 1, 2), date(2021, 4, 5))

        def close(day: date) -> float:
            if day < date(2020, 4, 1):
                return 10
            if day < date(2020, 7, 1):
                return 5
            if day < date(2021, 1, 1):
                return 50
            return 110

        result = simulate_v03_price_rules(
            entry_day=days[0],
            entry_price=10,
            prices=make_prices(days, close),
        )

        self.assertGreater(len(result.review_days), 0)
        self.assertEqual(
            [trade.rule for trade in result.trades],
            ["V3-P3", "V3-P4"],
        )
        self.assertEqual(
            [trade.fraction_of_q0 for trade in result.trades],
            [0.2, 0.3],
        )
        self.assertEqual(result.remaining_fraction, 0.5)

    def test_post_ten_x_drawdown_sells_the_remaining_half(self) -> None:
        days = weekdays(date(2020, 1, 2), date(2021, 1, 8))

        def close(day: date) -> float:
            if day < date(2020, 4, 1):
                return 10
            if day < date(2020, 9, 1):
                return 120
            return 50

        result = simulate_v03_price_rules(
            entry_day=days[0],
            entry_price=10,
            prices=make_prices(days, close),
        )

        self.assertEqual(
            [trade.rule for trade in result.trades],
            ["V3-P3", "V3-P4", "V3-P5"],
        )
        self.assertEqual(
            [trade.fraction_of_q0 for trade in result.trades],
            [0.2, 0.3, 0.5],
        )
        self.assertEqual(result.remaining_fraction, 0)

    def test_three_year_deadline_has_priority(self) -> None:
        days = weekdays(date(2020, 1, 2), date(2023, 1, 6))
        prices = make_prices(days, lambda _: 10)

        result = simulate_v03_price_rules(
            entry_day=days[0],
            entry_price=10,
            prices=prices,
        )

        self.assertEqual([trade.rule for trade in result.trades], ["V3-P2"])
        self.assertEqual(result.trades[0].fraction_of_q0, 1)
        self.assertEqual(result.remaining_fraction, 0)


if __name__ == "__main__":
    unittest.main()
