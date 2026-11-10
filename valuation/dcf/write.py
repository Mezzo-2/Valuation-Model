from __future__ import annotations

import argparse
from pathlib import Path

from valuation.comein.comein import ComeinClient
from valuation.dcf.calculate import calculate_dcf, valuation_date
from valuation.dcf.fetch import collect
from valuation.shared.io import dump_json, load_json, logs_dir, spec_dir


def run_dcf(run_dir, *, refresh=False):
    from valuation.workbook.load_json import load_spec
    root = spec_dir(run_dir)
    spec = load_spec(run_dir)
    path = root / 'dcf_inputs.json'
    if path.exists() and not refresh:
        inputs = load_json(path)
    else:
        facts = spec['facts']
        try:
            from valuation.dcf.assumptions import propose
            with ComeinClient() as mcp:
                collected = collect(facts, mcp, logs_dir(run_dir) / 'dcf')
                inputs = propose(spec, collected, mcp=mcp, run_dir=run_dir)
        except Exception as exc:
            # 外部服务失败只影响DCF，保留真实缺项；没有备用默认ERP或现金流比例。
            inputs = {'enabled': True, 'company': facts['company'], 'ticker': facts['ticker'],
                      'currency': facts.get('currency') or 'CNY', 'valuation_date': valuation_date(facts).isoformat(),
                      'open_gaps': [f'DCF输入获取失败：{type(exc).__name__}']}
        dump_json(path, inputs)
    result = calculate_dcf(spec, inputs)
    dump_json(root / 'dcf_results.json', result)
    print('dcf:', result['status'], f"price={result['price']:.2f}" if 'price' in result else '；'.join(result['gaps'][:3]))
    return result


def main():
    parser = argparse.ArgumentParser(description='补取并生成DCF假设；已有输入时仅重算')
    parser.add_argument('--run-dir', required=True)
    parser.add_argument('--refresh', action='store_true')
    args = parser.parse_args()
    run_dcf(Path(args.run_dir), refresh=args.refresh)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
