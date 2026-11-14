import argparse
import json
import os
import re
import sys
import shutil
import signal
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path


# cgroup v2 memory.stat 的关键字段。memory.current / memory.peak 包含可回收的
# 页缓存，单独看「peak 贴满物理内存」无法区分「进程真吃内存」与「编译中间
# 文件把空闲内存变成 page cache」。anon 才是会被换出的进程匿名页。
CGROUP_STAT_KEYS = (
    "anon", "file", "kernel_stack", "slab", "sock", "shmem",
    "file_mapped", "file_dirty", "file_writeback",
    "inactive_anon", "active_anon", "inactive_file", "active_file",
    "pgfault", "pgmajfault", "pgscan", "pgsteal",
)

LINKER_NAMES = {"ld", "ld.lld", "ld.gold", "gold", "lld", "mold", "collect2", "clang", "clang++", "cc"}
BUNDLER_HINTS = ("appimagetool", "linuxdeploy", "dpkg", "rpmbuild", "mksquashfs", "hdiutil", "codesign", "productbuild")


def read_text(path):
    try:
        return path.read_text().strip()
    except OSError:
        return None


def linux_memory():
    text = read_text(Path("/proc/meminfo"))
    if text is None:
        return {}
    values = {}
    for line in text.splitlines():
        key, _, raw = line.partition(":")
        if key in {"MemTotal", "MemAvailable", "SwapTotal", "SwapFree"}:
            values[key] = int(raw.split()[0]) * 1024
    return values


def parse_memory_stat(text):
    values = {}
    for line in (text or "").splitlines():
        key, _, raw = line.partition(" ")
        if key not in CGROUP_STAT_KEYS:
            continue
        try:
            values[key] = int(raw.strip())
        except ValueError:
            continue
    return values


def detect_cgroup_root():
    root = Path("/sys/fs/cgroup")
    text = read_text(Path("/proc/self/cgroup"))
    if text:
        relative = next((line[3:] for line in text.splitlines() if line.startswith("0::")), "")
        candidate = root / relative.lstrip("/")
        if (candidate / "memory.events").is_file():
            root = candidate
    return root


def cgroup_memory(root=None):
    root = Path(root) if root is not None else detect_cgroup_root()
    values = {}
    for name in ("memory.current", "memory.peak", "memory.max", "memory.events"):
        value = read_text(root / name)
        if value is not None:
            values[name] = value
    stat = read_text(root / "memory.stat")
    if stat is not None:
        values["memory.stat"] = parse_memory_stat(stat)
    return values


def memory_breakdown(cgroup):
    stat = cgroup.get("memory.stat") or {}
    if not stat:
        return {}
    anon = stat.get("anon", 0)
    file_cache = stat.get("file", 0)
    try:
        current = int(cgroup.get("memory.current", 0))
    except (TypeError, ValueError):
        current = 0
    return {
        "anon_mb": round(anon / 1048576),
        "file_cache_mb": round(file_cache / 1048576),
        "anon_pct_of_current": round(anon / current * 100, 1) if current > 0 else None,
        "pgscan": stat.get("pgscan"),
        "pgsteal": stat.get("pgsteal"),
    }


def safe_command_output(command):
    try:
        return subprocess.check_output(command, text=True, timeout=3, stderr=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return ""


def macos_memory():
    values = {}
    total = safe_command_output(["sysctl", "-n", "hw.memsize"]).strip()
    if total.isdigit():
        values["MemTotal"] = int(total)
    stats = safe_command_output(["vm_stat"])
    page = re.search(r"page size of (\d+) bytes", stats)
    if page:
        page_size = int(page.group(1))
        counts = {name.strip(): int(count) for name, count in re.findall(r"^([^:\n]+):\s+(\d+)\.", stats, re.M)}
        values["MemAvailable"] = sum(counts.get(key, 0) for key in ("Pages free", "Pages inactive", "Pages speculative")) * page_size
        values["Compressed"] = counts.get("Pages occupied by compressor", 0) * page_size
    swap = safe_command_output(["sysctl", "vm.swapusage"])
    for key, name in [("SwapTotal", "total"), ("SwapFree", "free")]:
        match = re.search(rf"{name}\s*=\s*([0-9.]+)([MGK])", swap)
        if match:
            values[key] = round(float(match.group(1)) * {"K": 1024, "M": 1048576, "G": 1073741824}[match.group(2)])
    return values


def macos_processes(limit=5):
    # comm is the executable name, never argv or environment variables.
    text = safe_command_output(["ps", "-axo", "pid=,rss=,pcpu=,comm="])
    entries = []
    for line in text.splitlines():
        fields = line.strip().split(None, 3)
        if len(fields) != 4:
            continue
        try:
            pid, rss, cpu = int(fields[0]), int(fields[1]), float(fields[2])
        except ValueError:
            continue
        name = Path(fields[3]).name
        if rss > 0:
            entries.append({"pid": pid, "name": name, "rss_mb": round(rss / 1024), "cpu_percent": cpu})
    # Keep build processes visible even when a system daemon has the largest RSS.
    relevant = [entry for entry in entries if infer_phase([entry]) != "unknown"]
    relevant.sort(key=lambda entry: entry["rss_mb"], reverse=True)
    others = sorted((entry for entry in entries if entry not in relevant), key=lambda entry: entry["rss_mb"], reverse=True)
    return (relevant + others)[:limit]


def top_processes(proc_root=None, limit=5):
    # 只读 /proc/<pid>/status 的 Name / State / VmRSS。故意不读 cmdline：
    # 构建命令行可能带签名密钥等敏感参数，绝不能进日志或 artifact。
    if proc_root is None and sys.platform == "darwin":
        return macos_processes(limit)
    root = Path(proc_root) if proc_root is not None else Path("/proc")
    if not root.is_dir():
        return []
    entries = []
    for status_path in root.glob("[0-9]*/status"):
        try:
            text = status_path.read_text()
            pid = int(status_path.parent.name)
        except (OSError, ValueError):
            continue
        name, state, rss_kb = "", "", 0
        for line in text.splitlines():
            if line.startswith("Name:"):
                name = line.partition(":")[2].strip()
            elif line.startswith("State:"):
                parts = line.split()
                if len(parts) > 1:
                    state = parts[1]
            elif line.startswith("VmRSS:"):
                parts = line.split()
                if len(parts) > 1:
                    try:
                        rss_kb = int(parts[1])
                    except ValueError:
                        pass
        if rss_kb > 0:
            entries.append({"pid": pid, "name": name, "state": state,
                            "rss_mb": round(rss_kb / 1024)})
    entries.sort(key=lambda entry: entry["rss_mb"], reverse=True)
    return entries[:limit]


def infer_phase(processes):
    names = [entry["name"].lower() for entry in processes]
    if any(any(hint in name for hint in BUNDLER_HINTS) for name in names):
        return "bundling"
    if any(name in LINKER_NAMES for name in names):
        return "linking"
    if any("rustc" in name for name in names):
        return "rustc-codegen"
    if any("cargo" in name for name in names):
        return "cargo"
    if any(name in {"node", "pnpm", "npm"} for name in names):
        return "frontend"
    return "unknown"


def snapshot(directory, cgroup_root=None, proc_root=None):
    disk = shutil.disk_usage(Path.cwd())
    cgroup = cgroup_memory(cgroup_root)
    processes = top_processes(proc_root)
    sample = {
        "at": datetime.now(timezone.utc).isoformat(),
        "memory": macos_memory() if sys.platform == "darwin" else linux_memory(),
        "cgroup_memory": cgroup,
        "memory_breakdown": memory_breakdown(cgroup),
        "processes": processes,
        "phase": infer_phase(processes),
        "disk": {"total": disk.total, "used": disk.used, "free": disk.free},
        "load": os.getloadavg() if hasattr(os, "getloadavg") else None,
    }
    with (directory / "resources.jsonl").open("a") as output:
        output.write(json.dumps(sample) + "\n")
    print("[build-diagnostics] " + json.dumps(sample), flush=True)
    return sample


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--interval", type=float, default=30)
    parser.add_argument("--timeout", type=float)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command or args.interval <= 0 or (args.timeout is not None and args.timeout <= 0):
        parser.error("a build command and positive interval/timeout are required")
    args.output_dir.mkdir(parents=True, exist_ok=True)
    process = None
    received_signal = None
    timed_out = False
    phase_seconds = {}

    def terminate_build(signum):
        if process is None:
            return
        try:
            if os.name == "posix":
                os.killpg(process.pid, signum)
            elif process.poll() is None:
                process.terminate()
            process.wait(timeout=5)
        except (ProcessLookupError, subprocess.TimeoutExpired):
            pass
        # pnpm can exit while a cargo/rustc descendant ignores the first signal.
        try:
            if os.name == "posix":
                os.killpg(process.pid, signal.SIGKILL)
            elif process.poll() is None:
                process.kill()
        except ProcessLookupError:
            pass
        process.wait()

    def shutdown(signum, _frame):
        nonlocal received_signal
        received_signal = signal.Signals(signum).name
        print(f"[build-diagnostics] received {received_signal}", flush=True)
        if process is not None and process.poll() is None:
            try:
                if os.name == "posix":
                    os.killpg(process.pid, signum)
                else:
                    process.terminate()
            except ProcessLookupError:
                pass

    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, shutdown)
    started = time.monotonic()
    last_sample = snapshot(args.output_dir)
    sample_at = time.monotonic()

    def timed_snapshot():
        nonlocal last_sample, sample_at
        now = time.monotonic()
        phase = last_sample.get("phase", "unknown")
        phase_seconds[phase] = phase_seconds.get(phase, 0) + now - sample_at
        last_sample = snapshot(args.output_dir)
        sample_at = now

    try:
        process = subprocess.Popen(command, start_new_session=os.name == "posix")
    except OSError as error:
        print(f"[build-diagnostics] could not start build: {error}", flush=True)
        exit_code = 127
    else:
        next_sample = time.monotonic() + args.interval
        while process.poll() is None:
            if received_signal is not None:
                terminate_build(signal.Signals[received_signal])
                break
            if args.timeout is not None and time.monotonic() - started >= args.timeout:
                timed_out = True
                print("[build-diagnostics] build timeout; stopping the process group", flush=True)
                terminate_build(signal.SIGTERM)
                break
            if time.monotonic() >= next_sample:
                timed_snapshot()
                next_sample = time.monotonic() + args.interval
            time.sleep(min(args.interval, 0.2))
        if received_signal is not None:
            terminate_build(signal.Signals[received_signal])
        exit_code = 128 + signal.Signals[received_signal] if received_signal is not None else 124 if timed_out else process.returncode if process.returncode >= 0 else 128 - process.returncode
    timed_snapshot()
    status = {
        "exit_code": exit_code,
        "received_signal": received_signal,
        "timed_out": timed_out,
        "phase_seconds": {phase: round(seconds, 3) for phase, seconds in phase_seconds.items()},
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "last_phase": last_sample.get("phase"),
        "last_memory_breakdown": last_sample.get("memory_breakdown"),
    }
    (args.output_dir / "status.json").write_text(json.dumps(status, indent=2) + "\n")
    print("[build-diagnostics] " + json.dumps(status), flush=True)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
