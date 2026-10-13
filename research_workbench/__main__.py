import argparse
import asyncio
import getpass
import json
import os
import re
from pathlib import Path

from .core import Config, collect

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "tmp" / "research_accounts.db"


async def execute(args):
    # This process only: pin the benchmark backend, do not change other tools.
    os.environ["TWS_HTTP_BACKEND"] = "httpx"
    from twscrape.accounts_pool import AccountsPool
    from twscrape.logger import set_log_level

    set_log_level("CRITICAL")
    DB.parent.mkdir(parents=True, exist_ok=True)
    lock = DB.parent / "research_worker.lock"
    # Setup and run share the same exclusive lock; no second account worker.
    fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    os.write(fd, str(os.getpid()).encode())
    os.close(fd)
    try:
        if args.command == "setup":
            pool = AccountsPool(str(DB))
            accounts = await pool.get_all()
            if accounts and (len(accounts) != 1 or accounts[0].username != args.account):
                raise ValueError("Existing database belongs to another account; do not overwrite")
            cookies = getpass.getpass(
                "Paste independent test-account Cookie header (hidden, local only): "
            )
            await pool.add_account_cookies(args.account, cookies)
            print("Test session saved locally in ignored tmp/. Never send Cookie in chat.")
            return
        from .transport import FixedAccountTransport

        config = Config(args.keyword, args.date, args.account, args.delay)
        async with FixedAccountTransport(DB, args.account) as transport:
            state = await collect(
                config,
                ROOT / "outputs" / args.run_id,
                transport,
                resume=args.resume,
                max_pages=args.max_pages,
            )
        print(
            json.dumps(
                {
                    "state": state["state"],
                    "saved": len(state["posts"]),
                    "pages": len(state["pages"]),
                    "search_end_verified": state["search_end_verified"],
                    "platform_all_posts_proven": False,
                    "output": f"outputs/{args.run_id}",
                },
                ensure_ascii=False,
            )
        )
    finally:
        lock.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(
        description="Isolated twscrape research pilot; no production profiles"
    )
    commands = parser.add_subparsers(dest="command", required=True)
    setup = commands.add_parser("setup")
    setup.add_argument("--account", default="independent-test")
    run = commands.add_parser("run")
    run.add_argument("--account", default="independent-test")
    run.add_argument("--keyword", required=True)
    run.add_argument("--date", required=True)
    run.add_argument("--run-id", required=True)
    run.add_argument(
        "--max-pages", type=int, default=5, help="Per invocation budget, not proof of completion"
    )
    run.add_argument("--delay", type=float, default=4.0)
    run.add_argument("--resume", action="store_true")
    args = parser.parse_args()
    if not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", args.account):
        parser.error("account must be a safe local alias, not a credential")
    if args.command == "run" and not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", args.run_id):
        parser.error("run-id must be a safe folder name, no paths")
    try:
        asyncio.run(execute(args))
    except KeyboardInterrupt:
        print("Paused; inspect saved state before resuming.")
    except Exception as exc:
        # Do not echo credential-related exception content into a public screen/log.
        parser.exit(
            1,
            f"Pilot failed safely: {type(exc).__name__}. Check local state; no account rotation.\n",
        )


if __name__ == "__main__":
    main()
