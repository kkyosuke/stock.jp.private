"""Read-only distinction between collected data, completed PAPER and LIVE gates."""
from pathlib import Path
import argparse
import json
try:
    from scripts.live_gate_evidence import evaluate_all_live_requirements, evaluate_official_coverage
    from scripts.limited_live import evaluate_limited_live_plan
    from scripts.market_regime import derive_market_regime
    from scripts.market_data_storage import resolve_archive_root
    from scripts.operation_state import validate_workspace
except ModuleNotFoundError:
    from live_gate_evidence import evaluate_all_live_requirements, evaluate_official_coverage
    from limited_live import evaluate_limited_live_plan
    from market_regime import derive_market_regime
    from market_data_storage import resolve_archive_root
    from operation_state import validate_workspace


def status(root):
    root=Path(root);private=root/'operations/private';gates=evaluate_all_live_requirements(root=root)
    coverage=evaluate_official_coverage(root=root)
    market={'status':'UNAVAILABLE','reason':'no saved market-regime input'}
    inputs=sorted((private/'research-inputs/market-regime').glob('*.json'))
    if inputs:
        try:
            _,result,_=derive_market_regime(json.loads(inputs[-1].read_text()),archive_root=resolve_archive_root(root))
            market={'status':result.state,'input_date':inputs[-1].stem}
        except (ValueError,KeyError,OSError) as error:market={'status':'UNAVAILABLE','reason':str(error)}
    limited=evaluate_limited_live_plan(root=root)
    return {'state_errors':validate_workspace(root),'official_paper_run_complete':coverage['eligible'],
            'official_source_blockers':coverage['blockers'],'market_regime':market,
            'live_gates':{k:{'eligible':v['eligible'],'blockers':v['blockers']} for k,v in gates.items()},
            'limited_live':{'eligible':limited['eligible'],'blockers':limited['blockers']},
            'decision':'WAIT','broker_submission':'HUMAN_ONLY',
            'note':'Read-only diagnostic; does not start/finalize PAPER, acquire data, or authorize orders.'}


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--root',type=Path,required=True);a=p.parse_args()
    print(json.dumps(status(a.root),ensure_ascii=False,indent=2));return 0


if __name__=='__main__':raise SystemExit(main())
