"""Command line: python -m accession <command>

    serve        web interface + embedded worker (default http://127.0.0.1:8000)
    worker       a standalone worker process (run several for more throughput)
    add          add domains to the register (optionally scan right away)
    scan         start a scan for a domain
    archive      queue never-submitted URLs of a domain
    status       print the register
    export       write the full inventory to CSV or JSON
    demo-site    serve the bundled test website
"""
import argparse
import logging
import signal
import sys
import time


def main(argv=None):
    ap = argparse.ArgumentParser(prog="accession", description="Website archive submitter and backup repository")
    sub = ap.add_subparsers(dest="cmd")

    p = sub.add_parser("serve", help="run the web interface and an embedded worker")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8000)
    p.add_argument("--no-worker", action="store_true", help="web interface only; run workers separately")

    p = sub.add_parser("worker", help="run a standalone worker")
    p.add_argument("--role", choices=["all", "crawl", "archive"], default="all")

    p = sub.add_parser("add", help="add one or more domains")
    p.add_argument("domains", nargs="+")
    p.add_argument("--services", default="wayback,local", help="comma separated: wayback,archive_today,local")
    p.add_argument("--max-pages", type=int, default=5000)
    p.add_argument("--scan", action="store_true", help="start scanning immediately")

    for name in ("scan", "archive"):
        p = sub.add_parser(name)
        p.add_argument("domain")

    sub.add_parser("status", help="print every domain with its counters")

    p = sub.add_parser("export")
    p.add_argument("path", help="file ending in .csv or .json")
    p.add_argument("--domain")

    p = sub.add_parser("demo-site", help="serve the bundled demo website")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--latency", type=float, default=0.0, help="seconds to wait before each answer")

    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    if args.cmd in (None, "serve"):
        import os

        import uvicorn

        if getattr(args, "no_worker", False):
            os.environ["ACCESSION_NO_WORKER"] = "1"
        host, port = getattr(args, "host", "127.0.0.1"), getattr(args, "port", 8000)
        print(f"Accession on http://{host}:{port}/")
        uvicorn.run("accession.web.app:app", host=host, port=port, log_level="warning")
        return

    if args.cmd == "demo-site":
        from .demo_site import serve

        serve(args.port, latency=args.latency)
        return

    from . import db, repo

    con = db.conn()
    db.init_db(con)

    if args.cmd == "worker":
        from .worker import Engine

        eng = Engine(role=args.role)
        signal.signal(signal.SIGTERM, lambda *_: setattr(eng, "stopping", True))
        eng.start_in_thread()
        print(f"worker {eng.worker_id} running ({args.role}); Ctrl+C to stop")
        try:
            while eng._thread.is_alive():
                time.sleep(0.5)
        except KeyboardInterrupt:
            print("stopping - handing held work back to the queue")
            eng.stop(15)
        return

    def find(text):
        from .urls import parse_domain_input

        parsed = parse_domain_input(text)
        row = db.one(con, "SELECT * FROM domains WHERE host = ? OR id = ?", parsed[0] if parsed else text, text)
        if not row:
            sys.exit(f"unknown domain: {text}")
        return row

    if args.cmd == "add":
        for text in args.domains:
            did, created = repo.add_domain(con, text, services=args.services, max_pages=args.max_pages)
            if did is None:
                print(f"skipped (not a URL): {text}")
                continue
            print(f"{'added' if created else 'exists'}: #{did} {text}")
            if args.scan:
                repo.start_scan(con, did, "cli")
        if args.scan:
            print("scans queued - a running worker or `serve` will pick them up")
    elif args.cmd == "scan":
        d = find(args.domain)
        print("scan queued" if repo.start_scan(con, d["id"], "cli") else "a scan is already running")
    elif args.cmd == "archive":
        d = find(args.domain)
        print(f"{db.plural(repo.enqueue_new(con, d['id']), 'submission')} queued")
    elif args.cmd == "status":
        rows = repo.domain_overview(con)
        print(f"{'domain':32} {'status':10} {'urls':>7} {'queued':>7} {'ok':>7} {'failed':>7}")
        for d in rows:
            print(f"{d['host'][:32]:32} {d['status']:10} {d['urls']:>7} {d['queued']:>7} {d['success']:>7} {d['failed']:>7}")
    elif args.cmd == "export":
        import csv
        import json

        from .web.app import EXPORT_COLS, export_rows

        did = find(args.domain)["id"] if args.domain else None
        rows = list(export_rows(con, did))
        with open(args.path, "w", newline="") as fh:
            if args.path.endswith(".json"):
                json.dump(rows, fh, indent=1)
            else:
                w = csv.DictWriter(fh, fieldnames=EXPORT_COLS)
                w.writeheader()
                w.writerows(rows)
        print(f"{len(rows)} rows written to {args.path}")


if __name__ == "__main__":
    main()
