"""Small synthetic layouts and isolated official numeric regression examples."""
from tests.operation_test_support import prepare_write
from datetime import date, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from io import BytesIO
import csv
import unittest
from unittest.mock import patch
from pypdf import PdfWriter
from pypdf.generic import DictionaryObject, NameObject, DecodedStreamObject

from stock_jp.market_data.regime_sources import MarketRegimeSourceError, _jpx_month_points, _jpx_index_points, _ci_vintage, _leading_ci, _jpx_physical_headers, _JPX_2024, _JPX_2025, _JPX_2026
from stock_jp.workspace import project_root
from stock_jp.strategy.regime import derive_market_regime
from stock_jp.operation.assessment import _existing_mrs

OLD_URL = 'https://www.jpx.co.jp/markets/statistics-equities/monthly/um3qrc000001r4m1-att/03_sisu2506.pdf'
NEW_URL = 'https://www.jpx.co.jp/markets/statistics-equities/monthly/t13vrt000001ul01-att/03_sisu2607.pdf'
HEADERS = 'TOPIX JPX Prime 150 Index Tokyo Stock Exchange Growth Market 250 Index REIT Index Infrastructure Funds Index Standard Market Growth Market'
OLD = 'Stock Price Index & Stock Price Average(End of Year/Month/Daily)\n' + HEADERS
NEW = 'Stock Price Index(End of Year/Month/Daily)\n' + HEADERS + ' JPX Start-Up Acceleration 100 Index'


def pdf(*pages):
    return SimpleNamespace(pages=[SimpleNamespace(extract_text=lambda text=t: text) for t in pages])


def ruled_pdf(headers, *, merged_averages=False):
    """Small real PDF with synthetic, physically positioned header/data cells."""
    writer = PdfWriter()
    page = writer.add_blank_page(width=20 + 90 * (len(headers) + 1), height=300)
    font = DictionaryObject({NameObject('/Type'): NameObject('/Font'),
                             NameObject('/Subtype'): NameObject('/Type1'),
                             NameObject('/BaseFont'): NameObject('/Helvetica')})
    page[NameObject('/Resources')] = DictionaryObject({NameObject('/Font'): DictionaryObject({
        NameObject('/F1'): writer._add_object(font)})})
    commands = ['0.5 w']
    edges = [10 + 90 * i for i in range(len(headers) + 2)]
    for i, x in enumerate(edges):
        top = 225 if merged_averages and i in {12, 14} else 260
        commands.append(f'{x} 130 m {x} {top} l S')
    for y in (130, 150, 260):
        commands.append(f'10 {y} m {edges[-1]} {y} l S')
    if merged_averages:
        commands.append(f'{edges[11]} 225 m {edges[-1]} 225 l S')
        for i, name in ((11, 'Prime Market'), (13, 'Standard Market')):
            commands.append(f'BT /F1 8 Tf 1 0 0 1 {edges[i]+4} 245 Tm ({name}) Tj ET')
    for i, label in enumerate(['Year, Month or Day', *headers]):
        top = 248
        if merged_averages and i >= 11:
            label = label.split(' Market ', 1)[1]
            top = 214
        for j, word in enumerate(label.split()):
            commands.append(f'BT /F1 8 Tf 1 0 0 1 {edges[i]+4} {top-j*11} Tm ({word}) Tj ET')
        value = '2025' if i == 0 else f'{100+i}.00'
        commands.append(f'BT /F1 8 Tf 1 0 0 1 {edges[i]+4} 137 Tm ({value}) Tj ET')
    stream = DecodedStreamObject()
    stream.set_data('\n'.join(commands).encode('ascii'))
    page[NameObject('/Contents')] = writer._add_object(stream)
    out = BytesIO()
    writer.write(out)
    return out.getvalue()


def english_labels(columns):
    labels = []
    for name in columns:
        if name == 'Growth250':
            name = 'Tokyo Stock Exchange Growth Market 250 Index'
        elif ' average' in name:
            name = name.replace(' weighted average', ' Market Weighted Stock Price Average').replace(
                ' arithmetic average', ' Market Arithmetic Stock Price Average')
        labels.append(name)
    return labels


class IndexLayoutTest(unittest.TestCase):
    def parse(self, text, url, headers=None):
        labels = english_labels(_JPX_2025 if url == OLD_URL else _JPX_2026) if headers is None else headers
        with patch('stock_jp.market_data.regime_sources.PdfReader', return_value=pdf(text)):
            return _jpx_month_points(ruled_pdf(labels), url)

    def test_complete_physical_header_order_rejects_reordered_missing_extra(self):
        labels = english_labels(_JPX_2026)
        text = NEW + '\n7.1 ' + ' '.join(f'{100+i}.00' for i in range(11)) + '\n平均'
        swapped = labels.copy()
        swapped[7], swapped[8] = swapped[8], swapped[7]
        missing = labels.copy()
        missing[2] = ''
        unknown = labels.copy()
        unknown[2] += ' Unknown Extra Index'
        for bad in (swapped, missing, unknown, [*labels, 'Unknown Extra Index']):
            with self.subTest(headers=bad), self.assertRaises(MarketRegimeSourceError):
                self.parse(text, NEW_URL, bad)
        for columns in (_JPX_2024, _JPX_2025, _JPX_2026):
            self.assertEqual(_jpx_physical_headers(ruled_pdf(english_labels(columns))), columns)
        self.assertEqual(_jpx_physical_headers(ruled_pdf(english_labels(_JPX_2025), merged_averages=True)), _JPX_2025)

    def test_actual_published_pdfs_with_open_border_currency_and_wrapped_headers(self):
        directory = project_root() / "research/validation-2026-10-05"
        import json
        register = json.loads((directory / "market-primary-sources.json").read_text())
        examples = {
            "2412": ("2024-12-30", 2784.92, 644.18),
            "2506": ("2025-06-02", 2777.29, 746.0),
            "2608": ("2026-08-31", 4156.29, 812.04),
        }
        for month, (day, topix_value, growth_value) in examples.items():
            with self.subTest(month=month):
                source = next(row for row in register["sources"]
                              if f"03_sisu{month}.pdf" in row["url"])
                topix, growth = _jpx_month_points(
                    (project_root() / source["path"]).read_bytes(), source["url"])
                self.assertEqual(next(row["value"] for row in topix if row["date"] == day), topix_value)
                self.assertEqual(next(row["value"] for row in growth if row["date"] == day), growth_value)

    def test_verified_old_growth_column_is_not_arithmetic_average(self):
        # June 2, 2025: only the target and misleading last cell are official;
        # every other cell below is synthetic, not a reproduced market series.
        text = OLD + '\n6.1\n2 100.00 101.00 102.00 103.00 104.00 105.00 106.00 746.00 108.00 109.00 110.00 111.00 112.00 1448.35\n平均'
        topix, growth = self.parse(text, OLD_URL)
        self.assertEqual(topix, [{'date': '2025-06-02', 'value': 100.0}])
        self.assertEqual(growth, [{'date': '2025-06-02', 'value': 746.0}])

    def test_verified_new_growth_column_is_not_infrastructure_index(self):
        text = NEW + '\n7.1\n31 100.00 101.00 102.00 103.00 104.00 105.00 106.00 107.00 685.97 109.00 664.75\n平均'
        self.assertEqual(self.parse(text, NEW_URL)[1][0]['value'], 685.97)

    def test_unknown_ambiguous_missing_extra_and_duplicate_rows_fail_closed(self):
        row = '2 ' + ' '.join(f'{100+i}.00' for i in range(14))
        valid = OLD + '\n6.1\n' + row + '\n平均'
        for bad in (valid.replace('Growth Market 250 Index', 'Other Index'),
                    valid.replace(OLD, OLD + '\n' + NEW),
                    valid.replace(row, row + ' 900.00'),
                    valid.replace(row, row.rsplit(' ', 1)[0]),
                    valid.replace(row, row + '\n' + row),
                    valid.replace('100.00', '---')):
            with self.subTest(bad=bad), self.assertRaises(MarketRegimeSourceError):
                self.parse(bad, OLD_URL)

    def test_same_month_pdf_cannot_supply_point_in_time_month_end(self):
        with patch('stock_jp.market_data.regime_sources._jpx_pdf_links', return_value=[NEW_URL]), \
             patch('stock_jp.market_data.regime_sources._download') as download:
            with self.assertRaises(MarketRegimeSourceError):
                _jpx_index_points(['synthetic'], as_of=date(2026, 7, 31), timeout=1)
            download.assert_not_called()

    def test_legacy_input_and_log_require_review_without_mutation(self):
        document = {'schema_version': '1.0', 'as_of': '2026-07-31',
                    'series': {'growth250': {'source_url': NEW_URL}}}
        with TemporaryDirectory() as directory:
            path = Path(directory) / 'log.csv'
            with prepare_write(path).open('w') as stream:
                writer = csv.writer(stream)
                writer.writerow(['as_of_jst', 'rule_version', 'source_urls'])
                writer.writerow(['2026-07-31', 'MRS-v0.1', NEW_URL])
            before = path.read_bytes()
            with self.assertRaisesRegex(ValueError, 're-extract'):
                derive_market_regime(document, archive_root=Path(directory))
            with self.assertRaisesRegex(MarketRegimeSourceError, 're-extraction'):
                _existing_mrs(path, '2026-07-31')
            self.assertEqual(path.read_bytes(), before)


class CiVintageTest(unittest.TestCase):
    def fixture(self, period, release, values):
        year, month = map(int, period.split('-'))
        ry, rm, rd = map(int, release.split('-'))
        months = [((month - 6 + i) % 12) + 1 for i in range(6)]
        return pdf(f'令 和 8 ({ry}) 年 {rm} 月 {rd} 日\n令和8(2026)年10月7日の予定です。',
                   ' '.join(f'{m}月' for m in months) + '\nＣＩ先行指数 ' + ' '.join(map(str, values)))

    def test_historical_vintages_before_at_after_1400_and_revisions(self):
        page = 'https://www.esri.cao.go.jp/jp/stat/di/di.html'
        vintages = {
            '202606rsummary.pdf': self.fixture('2026-06', '2026-08-25', [100, 101, 115.4, 103, 104, 116.5]),
            '202607report.pdf': self.fixture('2026-07', '2026-09-07', [100, 101, 115.9, 103, 104, 117.9]),
            '202607rsummary.pdf': self.fixture('2026-07', '2026-09-29', [100, 101, 115.9, 103, 104, 117.7]),
            '202608report.pdf': self.fixture('2026-08', '2026-10-07', [100, 101, 102, 103, 104, 105]),
        }
        html = ''.join(f'<a href="{name}">PDF</a>' for name in vintages).encode()
        def download(url, **_):
            return (html if url == page else url.rsplit('/', 1)[1].encode()), url
        def reader(stream):
            return vintages[stream.read().decode()]
        for cutoff, expected, latest, prior in (
            ('2026-08-25T13:59:59+09:00', None, None, None),
            ('2026-08-25T14:00:00+09:00', '202606rsummary.pdf', 116.5, 115.4),
            ('2026-09-07T13:59:59+09:00', '202606rsummary.pdf', 116.5, 115.4),
            ('2026-09-07T14:00:00+09:00', '202607report.pdf', 117.9, 115.9),
            ('2026-09-29T13:59:59+09:00', '202607report.pdf', 117.9, 115.9),
            ('2026-09-29T14:00:00+09:00', '202607rsummary.pdf', 117.7, 115.9),
            ('2026-09-29T14:00:01+09:00', '202607rsummary.pdf', 117.7, 115.9),
            ('2026-10-02T21:00:00+09:00', '202607rsummary.pdf', 117.7, 115.9),
        ):
            with self.subTest(cutoff=cutoff), patch('stock_jp.market_data.regime_sources._download', side_effect=download), \
                 patch('stock_jp.market_data.regime_sources.PdfReader', side_effect=reader):
                at = datetime.fromisoformat(cutoff)
                if expected is None:
                    with self.assertRaises(MarketRegimeSourceError):
                        _leading_ci(page, as_of=at.date(), cutoff_at=at, timeout=1)
                else:
                    points, url, published = _leading_ci(page, as_of=at.date(), cutoff_at=at, timeout=1)
                    self.assertTrue(url.endswith(expected))
                    self.assertEqual((points[-1]['value'], points[-4]['value']), (latest, prior))
                    self.assertTrue(published.endswith('T14:00:00+09:00'))

    def test_year_boundary_and_ambiguous_publication_rejected(self):
        fixture = self.fixture('2026-01', '2026-03-06', [100, 101, 102, 103, 104, 105])
        url = 'https://www.esri.cao.go.jp/jp/stat/di/202601report.pdf'
        with patch('stock_jp.market_data.regime_sources.PdfReader', return_value=fixture):
            points, _ = _ci_vintage(b'synthetic', url)
            self.assertEqual(points[0]['period'], '2025-08')
            self.assertEqual(points[-1]['period'], '2026-01')
        with patch('stock_jp.market_data.regime_sources.PdfReader', return_value=pdf('no publication date')):
            with self.assertRaises(MarketRegimeSourceError):
                _ci_vintage(b'synthetic', url)
