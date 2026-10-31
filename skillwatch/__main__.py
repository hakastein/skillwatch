"""python -m skillwatch [serve|backfill|queue|calibrate]"""
import argparse

PORT = 8770


def main() -> None:
    ap = argparse.ArgumentParser(prog='skillwatch')
    sub = ap.add_subparsers(dest='cmd')
    s = sub.add_parser('serve', help='service for the hook and the window (default)')
    s.add_argument('--no-review', action='store_true', help='accept writes without review (marked "unreviewed")')
    b = sub.add_parser('backfill', help='summarize ledger history that has no summary yet')
    b.add_argument('--limit', type=int, default=50, help='number of batches')
    c = sub.add_parser('calibrate', help='run the reviewer over the calibration set; model from reviewer.yaml or flags')
    c.add_argument('--provider')
    c.add_argument('--model')
    c.add_argument('--reasoning')
    q = sub.add_parser('queue', help='add edits to the calibration set, spread across skills, profiles and actors')
    q.add_argument('--size', type=int, default=100)
    args = ap.parse_args()
    if args.cmd == 'backfill':
        from .batch import backfill
        backfill(args.limit)
    elif args.cmd == 'calibrate':
        from .batch import calibrate
        calibrate(provider=args.provider, model=args.model, reasoning=args.reasoning)
    elif args.cmd == 'queue':
        from .batch import build_queue
        build_queue(args.size)
    else:
        import os
        import uvicorn
        if getattr(args, 'no_review', False):
            os.environ['SKILLWATCH_REVIEW'] = 'off'
        uvicorn.run('skillwatch.app:app', host='127.0.0.1', port=PORT, log_level='warning')


if __name__ == '__main__':
    main()
