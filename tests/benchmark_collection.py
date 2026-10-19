"""Offline collection benchmark; all provider/GitHub responses are synthetic.

Run with: python -m tests.benchmark_collection --output .work/collection-benchmark.json
The GitHub duration measures virtual pacing only, not network or Actions time.
"""
import argparse
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import time
from unittest.mock import patch

from stock_jp.automation import nightly as n, paper_checkpoint, rate
from stock_jp.market_data import storage
from stock_jp.market_data.jquants import ingest
from tests.test_nightly_market_data import NightlyMarketDataTest
from tests.test_paper_checkpoint import PaperCheckpointTest


def nightly():
    case = NightlyMarketDataTest()
    case.setUp()
    clock = [1800000000.0]
    waits = []

    def wait(seconds):
        waits.append(seconds)
        clock[0] += seconds

    request = rate.BudgetedTransport(case.repo, clock=lambda: clock[0], sleep=wait)
    # The fixture supplies successful CI/reviews. This measures collection and
    # publication persistence, excluding a real GraphQL review request.
    request.review_decision = lambda *args: None
    try:
        with patch.object(ingest, 'utc_now', return_value=case.fixture.at.isoformat()):
            result = n.execute(case.root, case.context, request, preview=True, at=case.fixture.at,
                               transport=case.fixture.transport, sleep=lambda _: None)
        return {'status': result['status'], 'provider_requests': len(case.fixture.calls),
                'github_calls': len(case.repo.calls),
                'github_writes': sum(method != 'GET' for method, _, _ in case.repo.calls),
                'github_blob_uploads': sum(method == 'POST' and path.endswith('/git/blobs')
                                           for method, path, _ in case.repo.calls),
                'simulated_pacing_seconds': round(sum(waits), 2)}
    finally:
        case.doCleanups()


def archive(count):
    with TemporaryDirectory() as folder:
        root = Path(folder)
        source = root / 'source'
        (source / 'jquants').mkdir(parents=True)
        for i in range(count):
            (source / 'jquants' / f'sample-{i}.json').write_text(json.dumps({'id': i, 'synthetic': True}))
        started = time.perf_counter()
        result = storage.pack(source, root / 'store')
        return {'files': result['file_count'], 'generation': result['generation'],
                'seconds': round(time.perf_counter() - started, 3)}


def paper():
    with TemporaryDirectory() as folder:
        root = Path(folder).resolve()
        repo, context = PaperCheckpointTest().root_and_context(root)
        source = root / 'data/market/primary-sources'
        source.mkdir(parents=True)
        for i in range(100):
            (source / f'{i}.json').write_text(json.dumps({'source': i, 'synthetic': True}))
        clock = [1800000000.0]
        waits = []

        def wait(seconds):
            waits.append(seconds)
            clock[0] += seconds

        request = rate.BudgetedTransport(repo, clock=lambda: clock[0], sleep=wait)
        result = paper_checkpoint.run(root, context, n.Git(request), save=True)
        return {'status': result['status'], 'files': result['file_count'], 'github_calls': len(repo.calls),
                'github_writes': sum(method != 'GET' for method, _, _ in repo.calls),
                'simulated_pacing_seconds': round(sum(waits), 2)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--files', type=int, default=1000)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    if args.files < 1:
        parser.error('--files must be positive')
    report = {'nightly': nightly(), 'paper': paper(), 'pack': archive(args.files)}
    raw = json.dumps(report, indent=2) + '\n'
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(raw)
    print(raw, end='')


if __name__ == '__main__':
    main()
