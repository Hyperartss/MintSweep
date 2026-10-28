"""Safety tests for MintSweep's core.

Run with:  python3 tests/test_core.py

The test sets HOME to a temporary directory *before* importing the package,
so every target resolves inside the sandbox and no real file is touched.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

SANDBOX = tempfile.mkdtemp(prefix="mintsweep-test-")
os.environ["HOME"] = SANDBOX

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from mintsweep import core  # noqa: E402


def make(path: Path, size: int = 2048) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)


def main() -> int:
    sandbox = Path(SANDBOX)
    assert core.DELETE_ROOTS == (SANDBOX,), f"unsafe delete root: {core.DELETE_ROOTS}"

    thumb = sandbox / ".cache" / "thumbnails"
    pip_cache = sandbox / ".cache" / "pip"
    trash = sandbox / ".local" / "share" / "Trash" / "files"
    outside = sandbox / "keep-me" / "important.txt"

    # Caches below 1 MiB are deliberately not listed, so make these realistic.
    make(thumb / "a.png", 1_200_000)
    make(thumb / "b.png", 1_200_000)
    make(pip_cache / "wheel.whl", 1_500_000)
    make(trash / "old.txt", 1024)
    make(outside, 512)

    # A symlink in a cache pointing outside must never be followed.
    evil = sandbox / ".cache" / "evil"
    evil.symlink_to(sandbox / "keep-me", target_is_directory=True)

    targets = core.scan_targets(core.collect_targets())
    by_key = {t.key: t for t in targets}
    assert all(t.measured for t in targets), "scan_targets left targets unmeasured"
    assert "trash" in by_key, "trash target missing"
    assert "cache:thumbnails" in by_key, f"caches not discovered: {sorted(by_key)}"
    assert "cache:evil" not in by_key, "symlinked cache should be ignored"

    assert by_key["cache:thumbnails"].size == 2_400_000, by_key["cache:thumbnails"].size
    assert by_key["trash"].size == 1024, by_key["trash"].size

    # Nothing is preselected anywhere: the model has no default selection.
    assert not hasattr(core.Target, "default_on")

    # Refuse dangerous paths.
    assert not core.is_safe_to_delete(SANDBOX)
    assert not core.is_safe_to_delete("/")
    assert not core.is_safe_to_delete("/etc/passwd")
    assert core.is_safe_to_delete(str(thumb))

    # Cleaning exactly one target removes exactly that target.
    results = core.clean_targets([by_key["cache:thumbnails"]])
    assert results[0].ok and results[0].freed == 2_400_000, results
    assert not thumb.exists(), "selected cache was not removed"
    assert (pip_cache / "wheel.whl").exists(), "unselected cache was removed!"
    assert (trash / "old.txt").exists(), "unselected trash was removed!"
    assert outside.exists(), "file outside the targets was removed!"
    assert (sandbox / "keep-me").is_dir(), "symlink target was followed!"

    # Empty selection is a no-op, never a "clean everything".
    assert core.clean_targets([]) == []

    # Dry run reports but changes nothing.
    results = core.clean_targets([by_key["trash"]], dry_run=True)
    assert results[0].freed == 1024, results
    assert (trash / "old.txt").exists(), "dry run deleted files"

    # Trash cleaning removes files and metadata dir, nothing else.
    results = core.clean_targets([by_key["trash"]])
    assert results[0].ok, results
    assert not (trash / "old.txt").exists()
    assert outside.exists()

    # Memory helpers are read-only and must not raise.
    info = core.read_meminfo()
    assert info.total > 0
    procs = core.top_processes(limit=3)
    assert isinstance(procs, list)
    assert isinstance(core.end_processes([]), list)

    # Processes are grouped per app so a person picks "Brave", not 5 PIDs.
    groups = core.group_processes([
        core.ProcessInfo(1, "brave", 100, True),
        core.ProcessInfo(2, "brave", 50, True),
        core.ProcessInfo(3, "steam", 10, True),
    ])
    assert [g.name for g in groups] == ["brave", "steam"], groups
    assert groups[0].rss == 150 and groups[0].pids == [1, 2], groups[0]
    assert groups[0].instance_label == "2 processes", groups[0].instance_label
    assert groups[1].instance_label == "pid 3", groups[1].instance_label

    # Recommendations: only regenerable, worthwhile things are "recommended".
    def target(key, size):
        return core.Target(key=key, label=key, description="", category="c",
                           size=size, count=1, measured=True)

    targets = [target("cache:big", 200 * 1024 ** 2),
               target("cache:small", 5 * 1024 ** 2),
               target("trash", 1024 * 1024),
               target("journal", 100 * 1024 ** 2)]
    recs = {s.key: s for s in core.recommend_targets(targets)}
    assert recs["cache:big"].is_recommended, recs
    assert "cache:small" not in recs, "small caches should not be noise"
    assert recs["trash"].level == "review", "personal files are never 'recommended'"
    assert "journal" not in recs, "small journals are not worth recommending"
    assert target("trash", 1).size == 1

    # Recommendations for apps: big and idle only, never the desktop shell.
    groups = core.group_processes([
        core.ProcessInfo(10, "idleapp", 2 * 1024 ** 3, True, cpu_percent=0.1, age=7200),
        core.ProcessInfo(11, "busyapp", 2 * 1024 ** 3, True, cpu_percent=40.0, age=7200),
        core.ProcessInfo(12, "freshapp", 2 * 1024 ** 3, True, cpu_percent=0.0, age=60),
        core.ProcessInfo(13, "cinnamon", 2 * 1024 ** 3, True, cpu_percent=0.0, age=7200),
        core.ProcessInfo(14, "smallapp", 100 * 1024 ** 2, True, cpu_percent=0.0, age=7200),
    ])
    app_recs = {s.label for s in core.recommend_apps(groups)}
    assert app_recs == {"idleapp"}, app_recs

    # Restart advice fires on a nearly full swap, not on a healthy machine.
    full_swap = core.MemoryInfo(
        total=16 * 1024 ** 3, free=1 * 1024 ** 3, available=2 * 1024 ** 3,
        cached=1 * 1024 ** 3, buffers=0,
        swap_total=2 * 1024 ** 3, swap_free=100 * 1024 ** 2)
    advice = core.restart_advice(full_swap, uptime=3600)
    assert advice.recommended and any("swap" in r for r in advice.reasons), advice

    relaxed = core.MemoryInfo(
        total=16 * 1024 ** 3, free=8 * 1024 ** 3, available=12 * 1024 ** 3,
        cached=3 * 1024 ** 3, buffers=0,
        swap_total=2 * 1024 ** 3, swap_free=2 * 1024 ** 3)
    expected = os.path.exists(core.REBOOT_REQUIRED_FLAG)
    assert core.restart_advice(relaxed, uptime=600).recommended is expected

    old_pc = core.MemoryInfo(
        total=16 * 1024 ** 3, free=4 * 1024 ** 3, available=8 * 1024 ** 3,
        cached=4 * 1024 ** 3, buffers=0,
        swap_total=2 * 1024 ** 3, swap_free=1 * 1024 ** 3)
    assert core.restart_advice(old_pc, uptime=20 * 86400).recommended

    # ---- KeyValues parsing (Steam configs) ----
    parsed = core.parse_vdf('"AppState"\n{\n\t"name"\t\t"Test Game"\n'
                            '\t"nested"\n\t{\n\t\t"k"\t\t"v"\n\t}\n}\n')
    assert parsed["AppState"]["name"] == "Test Game", parsed
    assert parsed["AppState"]["nested"]["k"] == "v", parsed

    # ---- Fake Steam library ----
    steam = sandbox / ".steam" / "steam"
    steamapps = steam / "steamapps"
    steamapps.mkdir(parents=True)
    (steamapps / "libraryfolders.vdf").write_text(
        '"libraryfolders"\n{\n\t"0"\n\t{\n\t\t"path"\t\t"%s"\n\t}\n}\n' % steam)
    (steamapps / "appmanifest_123.acf").write_text(
        '"AppState"\n{\n\t"appid"\t\t"123"\n\t"name"\t\t"Test Game"\n'
        '\t"StateFlags"\t\t"4"\n\t"installdir"\t\t"TestGame"\n'
        '\t"SizeOnDisk"\t\t"5000000000"\n\t"LastPlayed"\t\t"0"\n}\n')
    config = steam / "userdata" / "1" / "config"
    config.mkdir(parents=True)
    (config / "localconfig.vdf").write_text(
        '"UserLocalConfigStore"\n{\n\t"apps"\n\t{\n\t\t"123"\n\t\t{\n'
        '\t\t\t"Playtime"\t\t"30"\n\t\t\t"LastPlayed"\t\t"1000000000"\n'
        '\t\t}\n\t}\n}\n')

    assert core.find_steam_root() == str(steam)
    games = core.steam_games()
    assert len(games) == 1, games
    assert games[0].name == "Test Game" and games[0].size == 5_000_000_000, games[0]
    assert games[0].playtime_minutes == 30, games[0]
    game_recs = core.recommend_games(games, now=2_000_000_000)
    assert len(game_recs) == 1 and game_recs[0].label == "Test Game", game_recs

    # ---- Big files, and moving them to Trash ----
    downloads = sandbox / "Downloads"
    downloads.mkdir(parents=True, exist_ok=True)
    big = downloads / "movie.mp4"
    with open(big, "wb") as handle:
        handle.truncate(300 * 1024 ** 2)  # sparse, instant
    old = 1_000_000.0
    os.utime(big, (old, old))

    skipped = downloads / "project" / ".git" / "pack.iso"
    skipped.parent.mkdir(parents=True, exist_ok=True)
    with open(skipped, "wb") as handle:
        handle.truncate(400 * 1024 ** 2)

    found = core.find_large_files(min_bytes=200 * 1024 ** 2, roots=[str(downloads)])
    paths = {item.path for item in found}
    assert str(big) in paths, paths
    assert str(skipped) not in paths, "must not descend into .git"

    file_recs = core.recommend_files(found, min_age=90 * 86400,
                                     now=old + 400 * 86400)
    assert any(s.key == f"file:{big}" for s in file_recs), file_recs
    assert all(not s.is_recommended for s in file_recs), "personal files are never auto-safe"

    moved = core.send_to_trash([str(big)])
    assert moved[0][1] is True, moved
    assert not big.exists(), "trashed file should be gone from Downloads"
    trash_file = sandbox / ".local" / "share" / "Trash" / "files" / "movie.mp4"
    assert trash_file.exists(), "file was not moved to Trash"
    info = sandbox / ".local" / "share" / "Trash" / "info" / "movie.mp4.trashinfo"
    assert info.exists() and "Path=" in info.read_text(), "trash metadata missing"

    # Same name again must not overwrite the first one.
    again = downloads / "movie.mp4"
    with open(again, "wb") as handle:
        handle.truncate(1024)
    core.send_to_trash([str(again)])
    assert (sandbox / ".local" / "share" / "Trash" / "files" / "movie.2.mp4").exists()
    assert trash_file.exists()

    # Trash refuses things outside home and refuses folders.
    assert core.send_to_trash(["/etc/hostname"])[0][1] is False
    download_dir = downloads / "folder"
    download_dir.mkdir()
    assert core.send_to_trash([str(download_dir)])[0][1] is False
    assert download_dir.is_dir()

    # ---- Unused apps ----
    apps = [
        core.InstalledApp("flatpak", "org.example.Idle", "Idle App",
                          3 * 1024 ** 3, old, removable=True),
        core.InstalledApp("apt", "idle-package", "Idle Package",
                          1024 ** 3, old, removable=False),
        core.InstalledApp("flatpak", "org.example.Fresh", "Fresh App",
                          3 * 1024 ** 3, 1_999_999_000, removable=True),
    ]
    app_recs = {s.label: s for s in core.recommend_unused_apps(apps, now=2_000_000_000)}
    assert set(app_recs) == {"Idle App", "Idle Package"}, app_recs
    assert app_recs["Idle App"].is_recommended
    assert not app_recs["Idle Package"].is_recommended

    print("all core safety tests passed")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    finally:
        shutil.rmtree(SANDBOX, ignore_errors=True)
