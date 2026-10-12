import argparse
import json
import sys
import time

from .audit import Budget, BudgetExceeded, Fetcher, audit_page


def main(argv=None):
    ap = argparse.ArgumentParser(prog="catalog-audit", description="Read-only audit of public product pages.")
    ap.add_argument("urls_file", help="text file, one URL per line (# comments allowed)")
    ap.add_argument("--max-requests", type=int, default=200)
    ap.add_argument("--max-seconds", type=float, default=120.0)
    ap.add_argument("--pause", type=float, default=2.0, help="seconds between pages")
    ap.add_argument("--allow-external-images", action="store_true")
    a = ap.parse_args(argv)
    f = Fetcher(Budget(a.max_requests, a.max_seconds))
    for line in open(a.urls_file, encoding="utf-8"):
        url = line.strip()
        if not url or url.startswith("#"):
            continue
        try:
            print(json.dumps(audit_page(url, f, a.allow_external_images), ensure_ascii=False))
        except BudgetExceeded:
            print(json.dumps({"stopped": "budget exhausted"}))
            return 1
        time.sleep(a.pause)
    return 0


if __name__ == "__main__":
    sys.exit(main())
