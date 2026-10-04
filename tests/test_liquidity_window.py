import unittest
from datetime import date,timedelta
from stock_jp.strategy.liquidity import trading_value_window


class LiquidityTest(unittest.TestCase):
    def setUp(self):
        self.cutoff=date(2026,10,2);self.calendar=[];self.bars=[]
        for i in range(59,-1,-1):
            day=self.cutoff-timedelta(days=i);open_day=day.weekday()<5
            self.calendar.append({'date':str(day),'holiday_division':'1' if open_day else '0'})
            if open_day:self.bars.append({'Date':str(day),'Code':'12340','O':1,'H':1,'L':1,'C':1,'Vo':100,'Va':100})
    def calc(self):return trading_value_window(self.bars,self.calendar,code='1234',cutoff=self.cutoff)
    def test_no_execution_includes_zero_without_extending_window(self):
        expected=self.calc()[0]
        self.bars[-5].update(dict.fromkeys(('O','H','L','C','Vo','Va')))
        days,values=self.calc();self.assertEqual(days,expected);self.assertEqual(sum(values)/20,95)
    def test_missing_invalid_and_latest_no_execution_fail(self):
        for edit in ('missing','partial','latest'):
            with self.subTest(edit=edit):
                self.setUp()
                if edit=='missing':self.bars.pop(-3)
                elif edit=='partial':self.bars[-3]['Va']=None
                else:self.bars[-1].update(dict.fromkeys(('O','H','L','C','Vo','Va')))
                with self.assertRaises(ValueError):self.calc()
    def test_calendar_gap_and_duplicate_fail(self):
        self.calendar.pop(-2)
        with self.assertRaises(ValueError):self.calc()
        self.setUp();self.calendar.append(self.calendar[-1])
        with self.assertRaises(ValueError):self.calc()
