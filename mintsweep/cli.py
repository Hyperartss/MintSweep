"""Command line front-end for MintSweep.

Run with no arguments to open the graphical window.
"""

from __future__ import annotations

import argparse
import os
import sys
import time

from . import __version__, core


def _print_table(targets: list[core.Target]) -> None:
    if not targets:
        print("Nothing found.")
        return
    width = max(len(t.label) for t in targets)
    for category in dict.fromkeys(t.category for t in targets):
        print(f"\n{category}")
        for target in targets:
            if target.category != category:
                continue
            size = core.human_size(target.size) if target.size else "—"
            extra = "  [admin]" if target.requires_root else ""
            print(f"  {target.label:<{width}}  {size:>9}{extra}")
            print(f"  {'':<{width}}  {target.description}")
            if target.note:
                print(f"  {'':<{width}}  note: {target.note}")
    print(f"\nTotal available: {core.human_size(core.total_size(targets))}")


def _cmd_scan(args: argparse.Namespace) -> int:
    targets = core.collect_targets(with_caches=not args.no_caches)
    cores = [t for t in targets if not t.measured]
    for index, target in enumerate(cores, start=1):
        print(f"\rMeasuring {index}/{len(cores)}: {target.label:<40}", end="", flush=True)
        target.measure()
    if cores:
        print("\r" + " " * 62 + "\r", end="")
    _print_table(targets)
    return 0


def _cmd_clean(args: argparse.Namespace) -> int:
    targets = core.collect_targets(with_caches=not args.no_caches)
    for target in targets:
        if not target.measured:
            target.measure()

    if args.recommended:
        suggestions = core.recommend_targets(targets)
        keys = {s.key for s in suggestions if s.is_recommended}
        chosen = [t for t in targets if t.key in keys and t.has_content]
        if not chosen:
            print("MintSweep has no recommendations right now.")
            return 0
    elif args.all:
        # Trash holds real files, so it is never swept up by --all.
        chosen = [t for t in targets if t.has_content and t.key != "trash"]
        if any(t.key == "trash" and t.has_content for t in targets):
            print("Skipping Trash (use --target trash if you really want it emptied).")
    elif args.target:
        wanted = set(args.target)
        chosen = [t for t in targets if t.key in wanted]
        missing = wanted - {t.key for t in chosen}
        for key in sorted(missing):
            print(f"Unknown or empty target: {key}", file=sys.stderr)
        if missing and not chosen:
            return 2
    else:
        print("Nothing selected. Pass --target KEY (repeatable) or --all.")
        print("Run 'mintsweep scan' to see the available keys.")
        return 2

    if not chosen:
        print("Nothing to clean.")
        return 0

    print("About to clean:")
    for target in chosen:
        print(f"  - {target.label}: {core.human_size(target.size)} ({core.human_count(target.count)})")

    needs_root = any(t.requires_root for t in chosen)
    if needs_root:
        print("  (one administrator password prompt for the system items)")

    if not args.yes and not args.dry_run:
        answer = input("Continue? [y/N] ").strip().lower()
        if answer not in ("y", "yes"):
            print("Cancelled. Nothing was changed.")
            return 1

    results = core.clean_targets(chosen, dry_run=args.dry_run)
    freed = 0
    failures = 0
    print()
    for result in results:
        if result.ok:
            freed += result.freed
            suffix = " (dry run)" if args.dry_run else ""
            print(f"  ✓ {result.target.label}: {core.human_size(result.freed)}{suffix}")
        else:
            failures += 1
            print(f"  ✗ {result.target.label}: {result.message}")

    print(f"\nFreed {core.human_size(freed)}." + (f" {failures} item(s) failed." if failures else ""))
    return 0 if failures == 0 else 1


def _cmd_recommend(args: argparse.Namespace) -> int:
    targets = core.scan_targets(core.collect_targets(with_caches=not args.no_caches))
    for index, target in enumerate(targets):
        print(f"\rMeasuring {index + 1}/{len(targets)}: {target.label:<40}", end="", flush=True)
    print("\r" + " " * 62 + "\r", end="")

    suggestions = core.recommend_targets(targets)
    worthwhile = [s for s in suggestions if s.is_recommended]
    review = [s for s in suggestions if not s.is_recommended]

    print("Suggested for removal (safe, and worth the space):")
    if worthwhile:
        for suggestion in worthwhile:
            print(f"  {suggestion.label:<32} {core.human_size(suggestion.size):>9}")
            print(f"  {'':<32} {suggestion.reason}")
        print(f"\n  Total: {core.human_size(sum(s.size for s in worthwhile))}")
    else:
        print("  Nothing worth removing right now.")

    if review:
        print("\nYour call (not recommended, because they are your own files):")
        for suggestion in review:
            print(f"  {suggestion.label:<32} {core.human_size(suggestion.size):>9}")
            print(f"  {'':<32} {suggestion.reason}")

    me = os.getpid()
    procs = [p for p in core.top_processes(limit=400) if p.owned_by_me and p.pid != me]
    apps = core.recommend_apps(core.group_processes(procs))
    print("\nApps that could be closed (big, but idle):")
    if apps:
        for suggestion in apps:
            print(f"  {suggestion.label:<32} {core.human_size(suggestion.size):>9}")
            print(f"  {'':<32} {suggestion.reason}")
    else:
        print("  None — everything big is busy doing something.")

    advice = core.restart_advice()
    print("\nRestart:")
    if advice.recommended:
        for reason in advice.reasons:
            print(f"  - {reason}")
        print("  A restart clears swap and any leaked memory. Save your work first.")
    else:
        print("  Not needed right now.")

    print("\nNothing was changed. Use 'mintsweep clean --recommended' to act on these.")
    return 0


def _cmd_files(args: argparse.Namespace) -> int:
    files = core.find_large_files(min_bytes=args.min_size * 1024 * 1024,
                                  limit=args.limit)
    stale = {s.key[5:] for s in core.recommend_files(files)}
    print(f"{len(files)} files bigger than {args.min_size} MB in your folders:")
    for item in files:
        used = time.strftime("%d %b %Y", time.localtime(item.last_used))
        mark = "  <- not used in 6+ months" if item.path in stale else ""
        print(f"  {core.human_size(item.size):>9}  {used}  {item.path}{mark}")
    print("\nNothing was changed. Files are only ever moved to Trash, from the app.")
    return 0


def _cmd_games(args: argparse.Namespace) -> int:
    games = core.steam_games()
    if not games:
        print("No Steam library found.")
        return 0
    suggestions = {s.key: s for s in core.recommend_games(games)}
    played = [g for g in games if g.playtime_minutes]
    print(f"{len(games)} Steam games installed, "
          f"{core.human_size(sum(g.size for g in games))} in total"
          f" ({len(played)} of them played at least once).")

    print("\nSuggestions (big and unplayed):")
    if suggestions:
        for suggestion in sorted(suggestions.values(), key=lambda s: s.size, reverse=True):
            print(f"  {suggestion.label:<34} {core.human_size(suggestion.size):>9}")
            print(f"  {'':<34} {suggestion.reason}")
    else:
        print("  None - everything big has been played recently.")

    print("\nBiggest games:")
    for game in games[:args.limit]:
        last = (time.strftime("%b %Y", time.localtime(game.last_played))
                if game.last_played else "never")
        hours = f"{game.playtime_hours:.0f} h" if game.playtime_minutes else "-"
        print(f"  {game.name[:34]:<34} {core.human_size(game.size):>9}  {hours:>7}  last {last}")

    print("\nMintSweep never deletes game files: uninstalling is done by Steam itself.")
    return 0


def _cmd_apps(args: argparse.Namespace) -> int:
    apps = core.list_installed_apps()
    if not apps:
        print("No applications found.")
        return 0
    suggestions = {s.key: s for s in core.recommend_unused_apps(apps)}
    print(f"{len(apps)} apps listed, {core.human_size(sum(a.size for a in apps))} total:")
    for app in apps[:args.limit]:
        used = (time.strftime("%d %b %Y", time.localtime(app.last_used))
                if app.last_used else "never")
        kind = "flatpak" if app.kind == "flatpak" else "system "
        mark = "  <- unused" if f"app:{app.kind}:{app.key}" in suggestions else ""
        print(f"  {app.name[:30]:<30} {core.human_size(app.size):>9}  {kind}  used {used}{mark}")

    if suggestions:
        print("\nUnused for a long time:")
        for suggestion in suggestions.values():
            print(f"  {suggestion.label:<30} {core.human_size(suggestion.size):>9}  {suggestion.reason}")
    else:
        print("\nNothing looks unused for more than six months.")

    print("\nFlatpaks can be removed from the app. System packages are for review only.")
    return 0


def _cmd_ram(args: argparse.Namespace) -> int:
    info = core.read_meminfo()
    print(f"Memory:  {core.human_size(info.used)} used of {core.human_size(info.total)}"
          f"  ({info.used_percent:.0f}%)")
    print(f"         {core.human_size(info.available)} available, "
          f"{core.human_size(info.cached)} cached, {core.human_size(info.free)} free")
    if info.swap_total:
        print(f"Swap:    {core.human_size(info.swap_used)} used of {core.human_size(info.swap_total)}"
              f"  ({info.swap_percent:.0f}%)")

    advice = core.restart_advice(info)
    print(f"\n{advice.summary}")

    print("\nBiggest processes by memory:")
    for proc in core.top_processes(limit=args.limit):
        mine = "" if proc.owned_by_me else "  (another user)"
        print(f"  {proc.pid:>7}  {proc.name[:32]:<32}  {core.human_size(proc.rss):>9}{mine}")

    if args.drop:
        print("\nReleasing page cache (administrator password)...")
        ok, message = core.drop_caches()
        if not ok:
            print(f"Could not release cache: {message}")
            return 1
        print("Page cache released. Note: cached memory is not wasted memory —"
              " it is reused automatically and this does not speed up the system.")
    return 0


def _cmd_close(args: argparse.Namespace) -> int:
    pids = args.pid
    for pid, ok, message in core.end_processes(pids):
        print(f"  {'✓' if ok else '✗'} {pid}: {message}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mintsweep",
        description="Choose exactly what to clean on Linux Mint. Nothing runs on its own.",
    )
    parser.add_argument("--version", action="version",
                        version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command")

    scan = sub.add_parser("scan", help="show what could be cleaned (read-only)")
    scan.add_argument("--no-caches", action="store_true", help="skip ~/.cache entries")
    scan.set_defaults(func=_cmd_scan)

    clean = sub.add_parser("clean", help="delete only the targets you name")
    clean.add_argument("--target", action="append", metavar="KEY",
                       help="target key from 'mintsweep scan' (repeatable)")
    clean.add_argument("--all", action="store_true", help="every non-empty target")
    clean.add_argument("--recommended", action="store_true",
                       help="only the targets MintSweep recommends")
    clean.add_argument("--dry-run", action="store_true", help="show what would happen")
    clean.add_argument("--yes", action="store_true", help="do not ask for confirmation")
    clean.add_argument("--no-caches", action="store_true", help="skip ~/.cache entries")
    clean.set_defaults(func=_cmd_clean)

    files = sub.add_parser("files", help="list big files in your folders (read-only)")
    files.add_argument("--min-size", type=int, default=200, metavar="MB")
    files.add_argument("--limit", type=int, default=40)
    files.set_defaults(func=_cmd_files)

    games = sub.add_parser("games", help="list Steam games with playtime and suggestions")
    games.add_argument("--limit", type=int, default=15)
    games.set_defaults(func=_cmd_games)

    apps = sub.add_parser("apps", help="list installed apps and long-unused ones")
    apps.add_argument("--limit", type=int, default=20)
    apps.set_defaults(func=_cmd_apps)

    rec = sub.add_parser("recommend", help="explain what is worth removing and why (read-only)")
    rec.add_argument("--no-caches", action="store_true", help="skip ~/.cache entries")
    rec.set_defaults(func=_cmd_recommend)

    ram = sub.add_parser("ram", help="show memory use and the biggest processes")
    ram.add_argument("--limit", type=int, default=12)
    ram.add_argument("--drop", action="store_true",
                     help="also release page cache (needs administrator password)")
    ram.set_defaults(func=_cmd_ram)

    close = sub.add_parser("close", help="ask the given processes to close")
    close.add_argument("pid", type=int, nargs="+")
    close.set_defaults(func=_cmd_close)

    return parser


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        from . import gui
        return gui.main()

    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 2
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
