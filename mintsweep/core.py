"""Core scanning and cleaning logic for MintSweep.

Design rules for this module:

* Nothing is ever deleted, closed or changed without an explicit list of
  targets passed in by the caller (the CLI or the GUI).
* Only a small set of well-known junk locations may be deleted, and only
  inside the user's home directory.  Everything else is measured and
  reported but never touched.
* Paths are validated before deletion, symlinks are never followed, and
  system-wide operations go through ``pkexec`` so the user sees a polkit
  password prompt.
"""

from __future__ import annotations

import os
import re
import shutil
import signal
import stat
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Iterable, Optional, Sequence
from urllib.parse import quote

#: Populated from the environment, but every function below re-reads it via
#: :func:`home_dir` so tests can relocate it.
HOME = os.path.expanduser("~")

#: The only place MintSweep is willing to delete files from.
DELETE_ROOTS: tuple[str, ...] = (HOME,)

APT_CACHE = "/var/cache/apt/archives"
JOURNAL_DIR = "/var/log/journal"

#: How much journal we keep when the user asks to trim logs.
JOURNAL_KEEP = "200M"

#: Caches smaller than this are not worth listing.
MIN_CACHE_BYTES = 1024 * 1024

ProgressFn = Callable[[int, int, "Target"], None]

try:
    CLOCK_TICKS = int(os.sysconf("SC_CLK_TCK"))
except (AttributeError, ValueError, OSError):  # pragma: no cover - non-POSIX
    CLOCK_TICKS = 100

#: A cache smaller than this is not worth recommending; it is noise.
CACHE_RECOMMEND_BYTES = 100 * 1024 * 1024
APT_RECOMMEND_BYTES = 100 * 1024 * 1024
JOURNAL_RECOMMEND_BYTES = 500 * 1024 * 1024

#: Apps are only suggested for closing when they are big, old and idle.
APP_RECOMMEND_MIN_BYTES = 512 * 1024 * 1024
APP_RECOMMEND_MIN_AGE = 600.0
APP_RECOMMEND_MAX_CPU = 1.0

#: Closing any of these would disturb the desktop session itself.
CRITICAL_PROCESS_NAMES = frozenset({
    "cinnamon", "nemo", "muffin", "xorg", "xwayland", "gnome-shell",
    "plasmashell", "kwin_x11", "kwin_wayland", "mate-panel", "xfce4-panel",
    "lightdm", "sddm", "gdm", "systemd", "systemd-journal",
    "systemd-logind", "dbus-daemon", "dbus-broker", "pipewire",
    "pipewire-pulse", "pulseaudio", "wireplumber", "polkitd",
    "csd-background", "csd-media-keys", "csd-power", "csd-xrandr",
    "csd-wacom", "csd-automount", "csd-keyboard", "cinnamon-screensaver",
    "gnome-keyring-daemon", "ssh-agent", "at-spi2-registryd", "ibus-daemon",
    "xdg-desktop-portal", "xdg-desktop-portal-gtk", "xdg-desktop-portal-gnome",
    "mintreport", "mintsweep",
})


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def home_dir() -> str:
    return os.path.expanduser("~")


def human_size(num_bytes: float) -> str:
    """Format a byte count the way ``df -h`` does (1024-based)."""
    size = float(num_bytes)
    for unit in ("B", "kB", "MB", "GB", "TB"):
        if abs(size) < 1024 or unit == "TB":
            if unit == "B":
                return f"{int(size)} B"
            return f"{size:.1f} {unit}"
        size /= 1024.0
    return f"{size:.1f} TB"


def human_count(num: int) -> str:
    return f"{num:,} file" if num == 1 else f"{num:,} files"


def human_duration(seconds: float) -> str:
    seconds = int(max(0, seconds))
    if seconds < 90:
        return f"{seconds} seconds"
    if seconds < 5400:
        return f"{round(seconds / 60)} minutes"
    if seconds < 172800:
        return f"{round(seconds / 3600)} hours"
    days = seconds / 86400
    if days < 60:
        return f"{round(days)} days"
    if days < 730:
        return f"{round(days / 30)} months"
    return f"{round(days / 365)} years"


def measure_path(path: str) -> tuple[int, int]:
    """Return ``(bytes, file_count)`` for *path*.

    Symlinks are counted as zero bytes and never followed, so measuring a
    folder can never escape into the rest of the filesystem.
    """
    try:
        st = os.lstat(path)
    except OSError:
        return 0, 0

    if stat.S_ISLNK(st.st_mode):
        return 0, 0
    if not stat.S_ISDIR(st.st_mode):
        return st.st_size, 1

    total = 0
    count = 0
    stack = [path]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as entries:
                for entry in entries:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(entry.path)
                        elif entry.is_file(follow_symlinks=False):
                            total += entry.stat(follow_symlinks=False).st_size
                            count += 1
                    except OSError:
                        continue
        except OSError:
            continue
    return total, count


def is_safe_to_delete(path: str) -> bool:
    """Refuse anything that is not strictly inside an allowed root."""
    if not path or path in ("/", "~"):
        return False
    real = os.path.realpath(path)
    for root in DELETE_ROOTS:
        root_real = os.path.realpath(root)
        if real == root_real:
            continue  # never delete the home directory itself
        if real.startswith(root_real + os.sep):
            return True
    return False


def remove_path(path: str) -> None:
    """Delete *path* without ever following symlinks."""
    if os.path.islink(path) or not os.path.isdir(path):
        os.unlink(path)
        return
    for entry in os.scandir(path):
        if entry.is_dir(follow_symlinks=False):
            remove_path(entry.path)
        else:
            os.unlink(entry.path)
    os.rmdir(path)


# --------------------------------------------------------------------------
# Targets
# --------------------------------------------------------------------------

@dataclass
class Target:
    """One thing the user may choose to clean."""

    key: str
    label: str
    description: str
    category: str
    paths: tuple[str, ...] = ()
    #: Root commands to run instead of deleting files, e.g. ("apt-get", "clean").
    root_commands: tuple[Sequence[str], ...] = ()
    requires_root: bool = False
    size: int = 0
    count: int = 0
    measured: bool = False
    #: Human-readable note shown in the GUI, e.g. "keeps the newest 200 MB".
    note: str = ""

    @property
    def has_content(self) -> bool:
        return self.size > 0

    def measure(self) -> "Target":
        total = 0
        count = 0
        for path in self.paths:
            part_bytes, part_count = measure_path(path)
            total += part_bytes
            count += part_count
        self.size = total
        self.count = count
        self.measured = True
        return self


CACHE_LABELS = {
    "thumbnails": "Thumbnail cache",
    "mozilla": "Firefox cache",
    "google-chrome": "Chrome cache",
    "chromium": "Chromium cache",
    "BraveSoftware": "Brave cache",
    "microsoft-edge": "Edge cache",
    "opera": "Opera cache",
    "vivaldi": "Vivaldi cache",
    "spotify": "Spotify cache",
    "pip": "Python (pip) downloads",
    "fontconfig": "Font cache",
    "mesa_shader_cache": "GPU shader cache",
    "tracker3": "File index cache",
    "evolution": "Evolution cache",
    "gvfs": "Network share cache",
    "nv": "NVIDIA driver cache",
    "libreoffice": "LibreOffice cache",
    "vlc": "VLC cache",
    "steam": "Steam cache",
}

#: Caches that are rebuilt automatically and are a plain waste of space.
CACHE_DESCRIPTION = (
    "Rebuilt automatically the next time you use the app, "
    "so the only cost is a slightly slower first start"
)


def builtin_targets() -> list[Target]:
    """Targets that are always offered, even when empty."""
    trash = os.path.join(home_dir(), ".local", "share", "Trash")
    return [
        Target(
            key="trash",
            label="Trash",
            description="Files you moved to Trash that are still taking up space",
            category="Files you deleted",
            paths=(os.path.join(trash, "files"), os.path.join(trash, "info")),
            note="Deleted files cannot be recovered afterwards",
        ),
        Target(
            key="apt-cache",
            label="Downloaded package cache",
            description="Old .deb files kept after installs; they can be downloaded again",
            category="System",
            paths=(APT_CACHE,),
            root_commands=(("apt-get", "clean"),),
            requires_root=True,
            note="Asks for your password",
        ),
        Target(
            key="journal",
            label="Old system logs",
            description="Systemd journal history. Only old entries are removed",
            category="System",
            paths=(JOURNAL_DIR,),
            root_commands=(("journalctl", f"--vacuum-size={JOURNAL_KEEP}"),),
            requires_root=True,
            note=f"Keeps the newest {JOURNAL_KEEP}",
        ),
    ]


def discover_cache_targets(
    min_bytes: int = MIN_CACHE_BYTES,
    limit: int = 25,
) -> list[Target]:
    """List the application caches found in ``~/.cache``, biggest first."""
    cache_root = os.path.join(home_dir(), ".cache")
    try:
        names = sorted(os.listdir(cache_root))
    except OSError:
        return []

    found: list[Target] = []
    for name in names:
        path = os.path.join(cache_root, name)
        if os.path.islink(path) or not os.path.isdir(path):
            continue
        size, count = measure_path(path)
        if size < min_bytes:
            continue
        label = CACHE_LABELS.get(name, f"{name} cache")
        found.append(
            Target(
                key=f"cache:{name}",
                label=label,
                description=CACHE_DESCRIPTION,
                category="Application caches",
                paths=(path,),
                size=size,
                count=count,
                measured=True,
            )
        )

    found.sort(key=lambda t: t.size, reverse=True)
    return found[:limit]


def collect_targets(with_caches: bool = True) -> list[Target]:
    """Everything the user can choose from, in display order."""
    targets = builtin_targets()
    if with_caches:
        targets.extend(discover_cache_targets())
    return targets


# --------------------------------------------------------------------------
# Scanning
# --------------------------------------------------------------------------

def scan_targets(targets: Iterable[Target], progress: Optional[ProgressFn] = None) -> list[Target]:
    """Measure every target that has not been measured yet. Read-only."""
    targets = list(targets)
    for index, target in enumerate(targets, start=1):
        if not target.measured:
            target.measure()
        if progress is not None:
            progress(index, len(targets), target)
    return targets


def total_size(targets: Iterable[Target]) -> int:
    return sum(t.size for t in targets)


# --------------------------------------------------------------------------
# Cleaning
# --------------------------------------------------------------------------

@dataclass
class CleanResult:
    target: Target
    freed: int = 0
    ok: bool = True
    message: str = ""


def _resolve_program(name: str) -> Optional[str]:
    found = shutil.which(name)
    if found:
        return found
    for prefix in ("/usr/bin/", "/bin/", "/usr/sbin/", "/sbin/"):
        candidate = prefix + name
        if os.path.exists(candidate):
            return candidate
    return None


def run_as_root(script: str) -> tuple[bool, str]:
    """Run a shell snippet through pkexec (polkit password prompt)."""
    pkexec = _resolve_program("pkexec")
    if not pkexec:
        return False, "pkexec is not available on this system"
    try:
        proc = subprocess.run(
            [pkexec, "/bin/sh", "-c", script],
            capture_output=True,
            text=True,
            timeout=180,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, str(exc)

    if proc.returncode == 0:
        return True, proc.stdout.strip()
    detail = (proc.stderr or proc.stdout or "").strip()
    if proc.returncode == 126:
        detail = detail or "Password prompt was cancelled"
    return False, detail or f"Command failed (exit {proc.returncode})"


def build_root_script(commands: Iterable[Sequence[str]]) -> str:
    parts = []
    for command in commands:
        program = _resolve_program(command[0])
        if program is None:
            continue
        argv = [program, *command[1:]]
        parts.append(" ".join(shlex_quote(part) for part in argv))
    return "; ".join(parts)


def shlex_quote(value: str) -> str:
    safe = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789@%_+=:,./-")
    if value and all(char in safe for char in value):
        return value
    return "'" + value.replace("'", "'\\''") + "'"


def clean_targets(
    targets: Iterable[Target],
    progress: Optional[ProgressFn] = None,
    dry_run: bool = False,
) -> list[CleanResult]:
    """Delete exactly the targets passed in and nothing else.

    The caller decides which targets to pass; this function has no notion of
    a default selection and will happily receive an empty list.
    """
    targets = list(targets)
    results: list[CleanResult] = []
    root_commands: list[Sequence[str]] = []
    root_targets: list[Target] = []

    for target in targets:
        if target.requires_root:
            if target.root_commands:
                root_commands.extend(target.root_commands)
                root_targets.append(target)
            else:
                results.append(CleanResult(target, ok=False, message="Needs administrator rights"))
            continue

        if dry_run:
            results.append(CleanResult(target, freed=target.size, message="dry run"))
            continue

        freed = 0
        failed: list[str] = []
        for path in target.paths:
            if not os.path.lexists(path):
                continue
            if not is_safe_to_delete(path):
                failed.append(os.path.basename(path))
                continue
            size, _count = measure_path(path)
            try:
                remove_path(path)
                freed += size
            except OSError as exc:
                failed.append(f"{os.path.basename(path)}: {exc.strerror or exc}")
        if failed:
            results.append(
                CleanResult(target, freed=freed, ok=False, message="; ".join(failed))
            )
        else:
            results.append(CleanResult(target, freed=freed))

    if root_commands and not dry_run:
        script = build_root_script(root_commands)
        if script:
            ok, output = run_as_root(script)
        else:
            ok, output = False, "The system tools for these items were not found"
        for target in root_targets:
            if ok:
                results.append(CleanResult(target, freed=target.size))
            else:
                results.append(CleanResult(target, ok=False, message=output))
    elif root_commands and dry_run:
        for target in root_targets:
            results.append(CleanResult(target, freed=target.size, message="dry run"))

    order = {id(t): i for i, t in enumerate(targets)}
    results.sort(key=lambda r: order.get(id(r.target), 0))

    if progress is not None:
        for index, result in enumerate(results, start=1):
            progress(index, len(results), result.target)
    return results


def drop_caches() -> tuple[bool, str]:
    """Ask the kernel to release page cache. Requires admin rights."""
    return run_as_root("sync; echo 3 > /proc/sys/vm/drop_caches")


# --------------------------------------------------------------------------
# Disk and memory information (read-only)
# --------------------------------------------------------------------------

@dataclass
class DiskInfo:
    total: int
    used: int
    free: int
    percent: float


def disk_info(path: str = "/") -> DiskInfo:
    st = os.statvfs(path)
    total = st.f_blocks * st.f_frsize
    free = st.f_bavail * st.f_frsize
    used = total - free
    percent = (used / total * 100.0) if total else 0.0
    return DiskInfo(total=total, used=used, free=free, percent=percent)


@dataclass
class MemoryInfo:
    total: int = 0
    free: int = 0
    available: int = 0
    cached: int = 0
    buffers: int = 0
    swap_total: int = 0
    swap_free: int = 0

    @property
    def used(self) -> int:
        return max(0, self.total - self.available)

    @property
    def used_percent(self) -> float:
        return (self.used / self.total * 100.0) if self.total else 0.0

    @property
    def swap_used(self) -> int:
        return max(0, self.swap_total - self.swap_free)

    @property
    def swap_percent(self) -> float:
        return (self.swap_used / self.swap_total * 100.0) if self.swap_total else 0.0


def read_meminfo() -> MemoryInfo:
    values: dict[str, int] = {}
    try:
        with open("/proc/meminfo", "r", encoding="utf-8") as handle:
            for line in handle:
                key, _, rest = line.partition(":")
                parts = rest.split()
                if parts:
                    values[key.strip()] = int(parts[0]) * 1024
    except OSError:
        return MemoryInfo()

    return MemoryInfo(
        total=values.get("MemTotal", 0),
        free=values.get("MemFree", 0),
        available=values.get("MemAvailable", values.get("MemFree", 0)),
        cached=values.get("Cached", 0) + values.get("SReclaimable", 0),
        buffers=values.get("Buffers", 0),
        swap_total=values.get("SwapTotal", 0),
        swap_free=values.get("SwapFree", 0),
    )


@dataclass
class ProcessInfo:
    pid: int
    name: str
    rss: int
    owned_by_me: bool
    #: Lifetime average CPU use, which is what makes an app "idle".
    cpu_percent: float = 0.0
    age: float = 0.0


@dataclass
class AppGroup:
    """All processes of one application, so "Brave" is a single choice."""

    name: str
    rss: int
    pids: list[int]
    cpu_percent: float = 0.0
    age: float = 0.0

    @property
    def instance_label(self) -> str:
        if len(self.pids) == 1:
            return f"pid {self.pids[0]}"
        return f"{len(self.pids)} processes"


def group_processes(processes: Iterable[ProcessInfo]) -> list[AppGroup]:
    """Collapse processes that share a name, biggest total memory first."""
    groups: dict[str, AppGroup] = {}
    for proc in processes:
        group = groups.get(proc.name)
        if group is None:
            groups[proc.name] = AppGroup(
                name=proc.name, rss=proc.rss, pids=[proc.pid],
                cpu_percent=proc.cpu_percent, age=proc.age)
        else:
            group.rss += proc.rss
            group.pids.append(proc.pid)
            # An app counts as busy if any of its processes is busy.
            group.cpu_percent = max(group.cpu_percent, proc.cpu_percent)
            group.age = max(group.age, proc.age)
    return sorted(groups.values(), key=lambda g: g.rss, reverse=True)


def _uptime_seconds() -> float:
    try:
        with open("/proc/uptime", "r", encoding="utf-8") as handle:
            return float(handle.read().split()[0])
    except (OSError, ValueError, IndexError):
        return 0.0


def _process_times(pid: int) -> Optional[tuple[int, int, int]]:
    """Return ``(utime, stime, starttime)`` in clock ticks for *pid*."""
    try:
        with open(f"/proc/{pid}/stat", "r", encoding="utf-8", errors="replace") as handle:
            raw = handle.read()
    except OSError:
        return None
    end = raw.rfind(")")
    if end < 0:
        return None
    fields = raw[end + 2:].split()
    try:
        return int(fields[11]), int(fields[12]), int(fields[19])
    except (IndexError, ValueError):
        return None


def top_processes(limit: int = 12) -> list[ProcessInfo]:
    """Biggest processes by resident memory. Read-only."""
    me = os.getuid()
    uptime = _uptime_seconds()
    found: list[ProcessInfo] = []
    try:
        entries = os.listdir("/proc")
    except OSError:
        return []

    for entry in entries:
        if not entry.isdigit():
            continue
        pid = int(entry)
        try:
            with open(f"/proc/{pid}/status", "r", encoding="utf-8", errors="replace") as handle:
                name = ""
                rss = 0
                for line in handle:
                    if line.startswith("Name:"):
                        name = line.split(":", 1)[1].strip()
                    elif line.startswith("VmRSS:"):
                        rss = int(line.split()[1]) * 1024
        except (OSError, ValueError, IndexError):
            continue
        if rss <= 0:
            continue  # kernel thread or exited
        try:
            owner = os.stat(f"/proc/{pid}").st_uid
        except OSError:
            continue

        age = 0.0
        cpu_percent = 0.0
        times = _process_times(pid)
        if times is not None and uptime > 0:
            utime, stime, starttime = times
            age = max(0.0, uptime - starttime / CLOCK_TICKS)
            if age > 1.0:
                cpu_percent = ((utime + stime) / CLOCK_TICKS) / age * 100.0

        found.append(ProcessInfo(
            pid=pid, name=name or f"pid {pid}", rss=rss,
            owned_by_me=owner == me, cpu_percent=cpu_percent, age=age))

    found.sort(key=lambda p: p.rss, reverse=True)
    return found[:limit]


def end_processes(pids: Iterable[int]) -> list[tuple[int, bool, str]]:
    """Send SIGTERM to exactly the PIDs the user picked.

    Only processes owned by the current user are signalled; the kernel would
    refuse the rest anyway, and we do not want a cleaner escalating rights
    just to kill something.
    """
    me = os.getuid()
    results: list[tuple[int, bool, str]] = []
    for pid in pids:
        try:
            if os.stat(f"/proc/{pid}").st_uid != me:
                results.append((pid, False, "Owned by another user"))
                continue
            os.kill(pid, signal.SIGTERM)
            results.append((pid, True, "Asked to close"))
        except ProcessLookupError:
            results.append((pid, False, "Already closed"))
        except PermissionError:
            results.append((pid, False, "Not permitted"))
        except OSError as exc:
            results.append((pid, False, exc.strerror or str(exc)))
    return results


# --------------------------------------------------------------------------
# Recommendations
# --------------------------------------------------------------------------

@dataclass
class Suggestion:
    """An evidence-based hint.

    A suggestion never selects, deletes or closes anything by itself: it only
    explains why an item is worth a look. The GUI shows it as a badge and the
    CLI lists it; the user still has to choose it.
    """

    key: str
    label: str
    size: int
    level: str  # "recommended" = safe and worthwhile, "review" = your call
    reason: str

    @property
    def is_recommended(self) -> bool:
        return self.level == "recommended"


def recommend_targets(targets: Iterable[Target]) -> list[Suggestion]:
    """Explain which junk items are worth removing, and why.

    Rules are deliberately conservative: only things that rebuild themselves
    or that the system regenerates are ever marked *recommended*. Trash is
    always *review*, because those are the user's own files.
    """
    suggestions: list[Suggestion] = []
    for target in targets:
        if not target.has_content:
            continue

        if target.key == "trash":
            suggestions.append(Suggestion(
                target.key, target.label, target.size, "review",
                "These are files you deleted yourself — emptying Trash is permanent"))
            continue

        if target.key == "apt-cache":
            if target.size >= APT_RECOMMEND_BYTES:
                suggestions.append(Suggestion(
                    target.key, target.label, target.size, "recommended",
                    f"{human_size(target.size)} of installers for packages you already "
                    "have; they can be downloaded again"))
            continue

        if target.key == "journal":
            if target.size >= JOURNAL_RECOMMEND_BYTES:
                suggestions.append(Suggestion(
                    target.key, target.label, target.size, "recommended",
                    f"Old log history; the newest {JOURNAL_KEEP} is always kept"))
            continue

        if target.key.startswith("cache:") and target.size >= CACHE_RECOMMEND_BYTES:
            suggestions.append(Suggestion(
                target.key, target.label, target.size, "recommended",
                "Rebuilds itself the next time you use the app — "
                "only the first start afterwards is slower"))

    suggestions.sort(key=lambda s: (not s.is_recommended, -s.size))
    return suggestions


def recommend_apps(
    groups: Iterable[AppGroup],
    min_bytes: int = APP_RECOMMEND_MIN_BYTES,
    min_age: float = APP_RECOMMEND_MIN_AGE,
    max_cpu: float = APP_RECOMMEND_MAX_CPU,
) -> list[Suggestion]:
    """Suggest closing apps that hold a lot of RAM but are doing nothing.

    "Doing nothing" is measured, not guessed: the app's lifetime average CPU
    use must be under ``max_cpu`` percent and it must have been open for a
    while, so a freshly started app is never labelled idle.
    """
    suggestions: list[Suggestion] = []
    for group in groups:
        if group.name.lower() in CRITICAL_PROCESS_NAMES:
            continue
        if group.rss < min_bytes or group.age < min_age or group.cpu_percent > max_cpu:
            continue
        suggestions.append(Suggestion(
            f"app:{group.name}", group.name, group.rss, "recommended",
            f"{human_size(group.rss)} in memory, but only {group.cpu_percent:.1f}% CPU "
            f"over the {human_duration(group.age)} it has been open"))
    suggestions.sort(key=lambda s: s.size, reverse=True)
    return suggestions


# --------------------------------------------------------------------------
# Restart advice
# --------------------------------------------------------------------------

REBOOT_REQUIRED_FLAG = "/var/run/reboot-required"


@dataclass
class RestartAdvice:
    """Whether a reboot would actually help, and the evidence for it.

    A restart is the only thing that genuinely clears swap and leaked memory,
    so it is worth suggesting — but only when there is a concrete reason.
    """

    recommended: bool
    reasons: list[str]

    @property
    def summary(self) -> str:
        if not self.recommended:
            return "No restart needed — memory pressure is low."
        return "A restart would help: " + "; ".join(self.reasons) + "."


def restart_advice(info: Optional[MemoryInfo] = None,
                   uptime: Optional[float] = None) -> RestartAdvice:
    info = read_meminfo() if info is None else info
    uptime = _uptime_seconds() if uptime is None else uptime

    reasons: list[str] = []

    try:
        if os.path.exists(REBOOT_REQUIRED_FLAG):
            reasons.append("An installed update (often the kernel) is waiting for a restart")
    except OSError:
        pass

    if info.total and info.available < info.total * 0.10:
        reasons.append(
            f"Only {human_size(info.available)} of {human_size(info.total)} memory is left")

    if info.swap_total and info.swap_percent >= 60:
        reasons.append(
            f"swap is {info.swap_percent:.0f}% full ({human_size(info.swap_used)} pushed to disk)")

    if uptime >= 10 * 86400 and info.swap_used > 0:
        reasons.append(f"the PC has been running for {human_duration(uptime)}")

    return RestartAdvice(recommended=bool(reasons), reasons=reasons)


# --------------------------------------------------------------------------
# Real files: find large ones, and only ever move them to Trash
# --------------------------------------------------------------------------

#: Standard user folders are scanned; personal files are never deleted outright.
USER_SUBDIRS = ("Desktop", "Documents", "Downloads", "Videos", "Pictures", "Music")

#: Never walk into these: they are caches, tool internals or other features' turf.
SKIP_DIR_NAMES = frozenset({
    ".git", ".hg", ".svn", "node_modules", "__pycache__", ".venv", "venv",
    "env", ".cache", ".local", ".steam", ".var", ".config", ".npm", ".cargo",
    ".gradle", ".mozilla", ".thunderbird", "steamapps", ".wine", "snap",
    "AppData", "target", "build", ".godot", ".import",
})

FILE_MIN_BYTES = 200 * 1024 * 1024
FILE_STALE_SECONDS = 180 * 86400


@dataclass
class FileInfo:
    path: str
    size: int
    modified: float
    accessed: float

    @property
    def last_used(self) -> float:
        # atime can lag on relatime mounts, mtime never goes backwards.
        return max(self.modified, self.accessed)

    @property
    def name(self) -> str:
        return os.path.basename(self.path)

    @property
    def folder(self) -> str:
        return os.path.dirname(self.path)


def find_large_files(
    min_bytes: int = FILE_MIN_BYTES,
    limit: int = 200,
    roots: Optional[Iterable[str]] = None,
    progress: Optional[Callable[[str, int], None]] = None,
    should_stop: Optional[Callable[[], bool]] = None,
) -> list[FileInfo]:
    """Find big files in the standard user folders. Read-only."""
    if roots is None:
        roots = [os.path.join(home_dir(), name) for name in USER_SUBDIRS]

    found: list[FileInfo] = []
    visited = 0
    for root in roots:
        if not os.path.isdir(root) or os.path.islink(root):
            continue
        for dirpath, dirnames, filenames in os.walk(root, followlinks=False,
                                                    onerror=lambda _e: None):
            dirnames[:] = [d for d in dirnames
                           if not d.startswith(".") and d not in SKIP_DIR_NAMES]
            visited += 1
            if progress is not None and visited % 40 == 0:
                progress(os.path.basename(root), visited)
            if should_stop is not None and should_stop():
                found.sort(key=lambda f: f.size, reverse=True)
                return found[:limit]
            for name in filenames:
                full = os.path.join(dirpath, name)
                try:
                    st = os.lstat(full)
                except OSError:
                    continue
                if not stat.S_ISREG(st.st_mode) or st.st_size < min_bytes:
                    continue
                found.append(FileInfo(full, st.st_size, st.st_mtime, st.st_atime))

    found.sort(key=lambda f: f.size, reverse=True)
    return found[:limit]


def recommend_files(files: Iterable[FileInfo], min_age: float = FILE_STALE_SECONDS,
                    now: Optional[float] = None) -> list[Suggestion]:
    """Point out big files that have not been touched in a long time.

    Always level *review*: these are the user's own documents, so MintSweep
    never claims they are safe to delete.
    """
    now = time.time() if now is None else now
    suggestions: list[Suggestion] = []
    for file in files:
        idle = now - file.last_used
        if idle < min_age:
            continue
        suggestions.append(Suggestion(
            f"file:{file.path}", file.name, file.size, "review",
            f"Not used in {human_duration(idle)} — {human_size(file.size)}"))
    return suggestions


def send_to_trash(paths: Iterable[str]) -> list[tuple[str, bool, str]]:
    """Move files to the freedesktop Trash so they can be restored.

    MintSweep never unlinks a personal file. Only regular files inside the
    home folder are accepted; folders are refused.
    """
    trash_root = os.path.join(home_dir(), ".local", "share", "Trash")
    files_dir = os.path.join(trash_root, "files")
    info_dir = os.path.join(trash_root, "info")
    results: list[tuple[str, bool, str]] = []

    for path in paths:
        path = os.path.abspath(path)
        if not os.path.lexists(path):
            results.append((path, False, "File no longer exists"))
            continue
        if not is_safe_to_delete(path):
            results.append((path, False, "Outside your home folder"))
            continue
        if os.path.isdir(path) and not os.path.islink(path):
            results.append((path, False, "Folders are not moved"))
            continue

        try:
            os.makedirs(files_dir, exist_ok=True)
            os.makedirs(info_dir, exist_ok=True)

            base = os.path.basename(path) or "file"
            name = base
            stem, extension = os.path.splitext(base)
            counter = 1
            while (os.path.lexists(os.path.join(files_dir, name))
                   or os.path.exists(os.path.join(info_dir, name + ".trashinfo"))):
                counter += 1
                name = f"{stem}.{counter}{extension}"

            info_path = os.path.join(info_dir, name + ".trashinfo")
            with open(info_path, "w", encoding="utf-8") as handle:
                handle.write("[Trash Info]\n")
                handle.write(f"Path={quote(path)}\n")
                handle.write(f"DeletionDate={datetime.now().strftime('%Y-%m-%dT%H:%M:%S')}\n")

            try:
                shutil.move(path, os.path.join(files_dir, name))
            except OSError:
                if os.path.exists(info_path):
                    os.unlink(info_path)
                raise
            results.append((path, True, ""))
        except OSError as exc:
            results.append((path, False, exc.strerror or str(exc)))

    return results


# --------------------------------------------------------------------------
# Steam library
# --------------------------------------------------------------------------

STEAM_ROOTS = (
    "~/.steam/steam",
    "~/.local/share/Steam",
    "~/.steam/debian-installation",
    "~/.var/app/com.valvesoftware.Steam/.local/share/Steam",
)

GAME_MIN_BYTES = 2 * 1024 ** 3
GAME_STALE_SECONDS = 180 * 86400

_VDF_TOKEN = re.compile(r'"(?:[^"\\]|\\.)*"|[{}]')
_VDF_COMMENT = re.compile(r"(?m)^\s*//.*$")


@dataclass
class Game:
    appid: str
    name: str
    size: int
    install_dir: str
    playtime_minutes: int = 0
    last_played: float = 0.0
    added: float = 0.0

    @property
    def playtime_hours(self) -> float:
        return self.playtime_minutes / 60.0


def parse_vdf(text: str) -> dict:
    """Parse a Valve KeyValues file (Steam's .acf/.vdf configs)."""
    text = _VDF_COMMENT.sub("", text)
    stack: list[dict] = [{}]
    key: Optional[str] = None
    for match in _VDF_TOKEN.finditer(text):
        token = match.group(0)
        if token == "{":
            node: dict = {}
            if key is not None:
                stack[-1][key] = node
            stack.append(node)
            key = None
        elif token == "}":
            if len(stack) > 1:
                stack.pop()
            key = None
        elif key is None:
            key = token[1:-1]
        else:
            stack[-1][key] = token[1:-1]
            key = None
    return stack[0]


def find_steam_root() -> Optional[str]:
    for candidate in STEAM_ROOTS:
        path = os.path.expanduser(candidate)
        if os.path.isdir(os.path.join(path, "steamapps")):
            return path
    return None


def _read_vdf_file(path: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            return parse_vdf(handle.read())
    except OSError:
        return {}


def _steam_library_paths(root: str) -> list[str]:
    paths: list[str] = []
    for relative in ("steamapps/libraryfolders.vdf", "config/libraryfolders.vdf"):
        candidate = os.path.join(root, relative)
        if not os.path.isfile(candidate):
            continue
        data = _read_vdf_file(candidate)
        libraries = data.get("libraryfolders") or data.get("LibraryFolders") or {}
        for key, value in libraries.items():
            if not str(key).isdigit():
                continue
            if isinstance(value, dict):
                path = value.get("path")
                if path:
                    paths.append(path)
            elif isinstance(value, str):
                paths.append(value)  # very old format: "1" "/path"
        break
    if root not in paths:
        paths.append(root)
    return paths


def _collect_playtime(node: dict, out: dict[str, dict]) -> None:
    for key, value in node.items():
        if not isinstance(value, dict):
            continue
        if str(key).isdigit() and ("Playtime" in value or "LastPlayed" in value):
            entry = out.setdefault(str(key), {})
            entry["playtime"] = max(entry.get("playtime", 0), int(value.get("Playtime", 0) or 0))
            entry["last_played"] = max(
                entry.get("last_played", 0), int(value.get("LastPlayed", 0) or 0))
        _collect_playtime(value, out)


def _steam_playtime(root: str) -> dict[str, dict]:
    out: dict[str, dict] = {}
    userdata = os.path.join(root, "userdata")
    try:
        users = os.listdir(userdata)
    except OSError:
        return out
    for user in users:
        config = os.path.join(userdata, user, "config", "localconfig.vdf")
        if os.path.isfile(config):
            _collect_playtime(_read_vdf_file(config), out)
    return out


def steam_games() -> list[Game]:
    """Every fully installed Steam game, biggest first. Read-only."""
    root = find_steam_root()
    if root is None:
        return []
    playtime = _steam_playtime(root)
    games: list[Game] = []
    seen: set[str] = set()

    for library in _steam_library_paths(root):
        apps_dir = os.path.join(library, "steamapps")
        try:
            entries = os.listdir(apps_dir)
        except OSError:
            continue
        for entry in entries:
            if not (entry.startswith("appmanifest_") and entry.endswith(".acf")):
                continue
            manifest_path = os.path.join(apps_dir, entry)
            data = _read_vdf_file(manifest_path).get("AppState")
            if not isinstance(data, dict):
                continue
            if str(data.get("StateFlags", "")) != "4":
                continue  # only fully installed games
            appid = str(data.get("appid") or entry[len("appmanifest_"):-4])
            if appid in seen:
                continue
            seen.add(appid)
            stats = playtime.get(appid, {})
            try:
                added = os.path.getmtime(manifest_path)
            except OSError:
                added = 0.0
            games.append(Game(
                appid=appid,
                name=str(data.get("name") or f"App {appid}"),
                size=int(data.get("SizeOnDisk", 0) or 0),
                install_dir=os.path.join(apps_dir, "common", str(data.get("installdir", ""))),
                playtime_minutes=int(stats.get("playtime", 0)),
                last_played=float(max(stats.get("last_played", 0),
                                      int(data.get("LastPlayed", 0) or 0))),
                added=added,
            ))

    games.sort(key=lambda g: g.size, reverse=True)
    return games


def recommend_games(games: Iterable[Game], now: Optional[float] = None,
                    min_size: int = GAME_MIN_BYTES) -> list[Suggestion]:
    """Suggest games that take a lot of room and are not being played."""
    now = time.time() if now is None else now
    suggestions: list[Suggestion] = []
    for game in games:
        if game.size < min_size:
            continue
        if game.playtime_minutes == 0:
            if game.added and now - game.added < 30 * 86400:
                continue  # installed recently, give it a chance
            reason = "Never launched"
            if game.added:
                reason += f" and installed {human_duration(now - game.added)} ago"
        elif game.last_played and now - game.last_played >= GAME_STALE_SECONDS:
            reason = (f"Last played {human_duration(now - game.last_played)} ago, "
                      f"{game.playtime_hours:.0f} h in total")
        elif (game.playtime_minutes < 120 and game.last_played
              and now - game.last_played >= 90 * 86400):
            reason = (f"Only {game.playtime_minutes} minutes played, last opened "
                      f"{human_duration(now - game.last_played)} ago")
        else:
            continue
        suggestions.append(Suggestion(
            f"game:{game.appid}", game.name, game.size, "recommended", reason))
    suggestions.sort(key=lambda s: s.size, reverse=True)
    return suggestions


def open_steam_uninstall(appid: str) -> tuple[bool, str]:
    """Hand the uninstall to Steam, which shows its own confirmation."""
    opener = shutil.which("xdg-open")
    if opener is None:
        return False, "xdg-open is not available"
    try:
        subprocess.Popen(
            [opener, f"steam://uninstall/{appid}"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True)
        return True, "Steam will ask you to confirm"
    except OSError as exc:
        return False, str(exc)


# --------------------------------------------------------------------------
# Installed applications (Flatpak and APT)
# --------------------------------------------------------------------------

APP_STALE_SECONDS = 180 * 86400
APP_MIN_BYTES = 200 * 1024 * 1024

#: Packages whose removal would damage the desktop or the boot process.
PROTECTED_PACKAGE_PREFIXES = (
    "mint", "cinnamon", "mate-", "xfce", "xorg", "xserver", "lightdm", "sddm",
    "systemd", "dbus", "nvidia", "grub", "linux-", "ubuntu-", "initramfs",
    "network-manager", "pipewire", "pulseaudio", "polkit", "flatpak", "apt",
    "dpkg", "software-properties", "gir1.2-", "python3-", "lib",
)

DESKTOP_DIRS = (
    "/usr/share/applications",
    "/usr/local/share/applications",
    os.path.join(home_dir(), ".local", "share", "applications"),
)


@dataclass
class InstalledApp:
    kind: str          # "flatpak" or "apt"
    key: str           # application id or package name
    name: str
    size: int
    last_used: float   # 0 means "never opened as far as we can tell"
    removable: bool = False
    detail: str = ""


def _size_text_to_bytes(text: str) -> int:
    match = re.match(r"\s*([\d.,]+)\s*([A-Za-z]*)", text or "")
    if not match:
        return 0
    try:
        number = float(match.group(1).replace(",", "."))
    except ValueError:
        return 0
    unit = match.group(2).upper()
    if unit.startswith("K"):
        return int(number * 1024)
    if unit.startswith("M"):
        return int(number * 1024 ** 2)
    if unit.startswith("G"):
        return int(number * 1024 ** 3)
    if unit.startswith("T"):
        return int(number * 1024 ** 4)
    return int(number)


def _app_last_used(data_dir: str) -> float:
    """Newest write to an app's Flatpak data, which happens while it runs."""
    if not os.path.isdir(data_dir):
        return 0.0  # never launched: Flatpak creates this on first start
    newest = 0.0
    for child in ("cache", "config", "data"):
        try:
            newest = max(newest, os.stat(os.path.join(data_dir, child)).st_mtime)
        except OSError:
            continue
    if newest:
        return newest
    try:
        return os.stat(data_dir).st_mtime
    except OSError:
        return 0.0


def list_flatpak_apps() -> list[InstalledApp]:
    """Flatpak applications with size and last-used estimate. Read-only."""
    flatpak = shutil.which("flatpak")
    if flatpak is None:
        return []
    try:
        proc = subprocess.run(
            [flatpak, "list", "--app",
             "--columns=application,name,size,installation"],
            capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return []
    if proc.returncode != 0:
        return []

    var_app = os.path.join(home_dir(), ".var", "app")
    apps: list[InstalledApp] = []
    for line in proc.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) < 4:
            continue
        appid, name, size_text, installation = parts[0], parts[1], parts[2], parts[3]
        apps.append(InstalledApp(
            kind="flatpak", key=appid, name=name or appid,
            size=_size_text_to_bytes(size_text),
            last_used=_app_last_used(os.path.join(var_app, appid)),
            removable=True, detail=installation.strip()))
    apps.sort(key=lambda a: a.size, reverse=True)
    return apps


def _desktop_entries() -> Iterable[str]:
    seen: set[str] = set()
    for directory in DESKTOP_DIRS:
        try:
            names = sorted(os.listdir(directory))
        except OSError:
            continue
        for name in names:
            if not name.endswith(".desktop"):
                continue
            path = os.path.join(directory, name)
            if path not in seen:
                seen.add(path)
                yield path


def _parse_desktop_entry(path: str) -> Optional[dict[str, str]]:
    values: dict[str, str] = {}
    in_entry = False
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                line = line.strip()
                if line.startswith("["):
                    in_entry = line == "[Desktop Entry]"
                    continue
                if not in_entry or "=" not in line or line.startswith("#"):
                    continue
                key, _, value = line.partition("=")
                values[key.strip()] = value.strip()
    except OSError:
        return None
    if values.get("Hidden") == "true" or values.get("NoDisplay") == "true":
        return None
    if values.get("Type", "Application") != "Application":
        return None
    return values


def _desktop_binary(exec_line: str) -> Optional[str]:
    tokens = [t.strip('"') for t in exec_line.split() if not t.startswith("%")]
    if not tokens:
        return None
    if os.path.basename(tokens[0]) in ("env", "sh", "bash", "flatpak", "python3"):
        return None
    binary = tokens[0]
    if not os.path.isabs(binary):
        binary = shutil.which(binary) or ""
    if not (binary.startswith("/usr/") or binary.startswith("/opt/")):
        return None
    return binary


def _apt_package_sizes() -> dict[str, int]:
    dpkg = shutil.which("dpkg-query")
    if dpkg is None:
        return {}
    try:
        proc = subprocess.run(
            [dpkg, "-W", "-f=${Package}\t${Installed-Size}\n"],
            capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return {}
    sizes: dict[str, int] = {}
    for line in proc.stdout.splitlines():
        package, _, kib = line.partition("\t")
        try:
            sizes[package] = int(kib) * 1024
        except ValueError:
            continue
    return sizes


def _apt_owners(binaries: Iterable[str]) -> dict[str, str]:
    paths = sorted(set(binaries))
    dpkg = shutil.which("dpkg-query")
    if dpkg is None or not paths:
        return {}
    owners: dict[str, str] = {}
    for start in range(0, len(paths), 100):
        try:
            proc = subprocess.run(
                [dpkg, "-S", *paths[start:start + 100]],
                capture_output=True, text=True, timeout=60)
        except (OSError, subprocess.TimeoutExpired):
            continue
        for line in proc.stdout.splitlines():
            package_part, _, file_part = line.partition(": ")
            package = package_part.split(",")[0].strip()
            if package and file_part:
                owners[file_part] = package
    return owners


def list_apt_apps(min_bytes: int = 100 * 1024 * 1024) -> list[InstalledApp]:
    """GUI applications installed with APT, with a last-used estimate.

    Usage comes from the access time of the program that the menu launches,
    which is a decent proxy but not a guarantee, so these are never offered
    for automatic removal. Library and desktop packages are filtered out.
    """
    sizes = _apt_package_sizes()
    if not sizes:
        return []

    entries: list[tuple[str, str]] = []
    for path in _desktop_entries():
        values = _parse_desktop_entry(path)
        if not values or "Exec" not in values or "Name" not in values:
            continue
        binary = _desktop_binary(values["Exec"])
        if binary:
            entries.append((values["Name"], binary))

    owners = _apt_owners(binary for _name, binary in entries)
    apps: dict[str, InstalledApp] = {}
    for name, binary in entries:
        package = owners.get(binary)
        if not package or package.lower().startswith(PROTECTED_PACKAGE_PREFIXES):
            continue
        try:
            accessed = os.stat(binary).st_atime
        except OSError:
            continue
        existing = apps.get(package)
        if existing is None:
            apps[package] = InstalledApp(
                kind="apt", key=package, name=name, size=sizes.get(package, 0),
                last_used=accessed, removable=False, detail="system package")
        else:
            existing.last_used = max(existing.last_used, accessed)
            existing.size = max(existing.size, sizes.get(package, 0))

    return sorted((a for a in apps.values() if a.size >= min_bytes),
                  key=lambda a: a.size, reverse=True)


def list_installed_apps() -> list[InstalledApp]:
    return list_flatpak_apps() + list_apt_apps()


def recommend_unused_apps(apps: Iterable[InstalledApp], now: Optional[float] = None,
                          min_size: int = APP_MIN_BYTES,
                          stale: float = APP_STALE_SECONDS) -> list[Suggestion]:
    """Suggest apps that take space and show no sign of recent use."""
    now = time.time() if now is None else now
    suggestions: list[Suggestion] = []
    for app in apps:
        if app.size < min_size:
            continue
        if app.last_used == 0:
            reason = "Never opened"
        else:
            idle = now - app.last_used
            if idle < stale:
                continue
            reason = f"Not used in {human_duration(idle)}"
        if app.kind == "apt":
            reason += " (system package — remove it from Software Manager)"
        suggestions.append(Suggestion(
            f"app:{app.kind}:{app.key}", app.name, app.size,
            "recommended" if app.removable else "review", reason))
    suggestions.sort(key=lambda s: s.size, reverse=True)
    return suggestions


def uninstall_flatpak(app: InstalledApp) -> tuple[bool, str]:
    """Remove a Flatpak app. User data in ~/.var/app is kept."""
    flatpak = shutil.which("flatpak")
    if flatpak is None:
        return False, "flatpak is not installed"
    command = [flatpak, "uninstall", "-y"]
    if app.detail in ("system", "user"):
        command.append(f"--{app.detail}")
    command.append(app.key)
    try:
        proc = subprocess.run(command, capture_output=True, text=True, timeout=300)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, str(exc)
    if proc.returncode == 0:
        return True, proc.stdout.strip() or "Removed"
    return False, (proc.stderr or proc.stdout or "flatpak failed").strip()
