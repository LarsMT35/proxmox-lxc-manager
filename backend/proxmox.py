"""Proxmox command wrapper.

Two runners share one interface:
  * LocalRunner – the app runs directly on the Proxmox host, `pct` is local.
  * SSHRunner   – the app runs elsewhere and reaches the host via `ssh`.

Every command is built from an explicit argv list – NO shell string
interpolation with user input. CT IDs are validated as integers before use.
"""
from __future__ import annotations

import asyncio
import os
import re
import shlex
from dataclasses import dataclass
from typing import AsyncIterator, Callable

from .config import Config

APT_ENV = "DEBIAN_FRONTEND=noninteractive"

# Writable location for ssh's known_hosts. The systemd unit runs with
# ProtectHome=read-only, so /root/.ssh is not writable; /var/lib/... is.
_STATE_DIR = (os.path.dirname(os.environ.get(
    "LXC_COMMANDER_DB", "/var/lib/lxc-commander/commander.db"))
    or "/var/lib/lxc-commander")


@dataclass
class CmdResult:
    argv: list[str]
    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0


def _validate_ctid(ctid: int | str) -> int:
    ctid = int(ctid)
    if not (100 <= ctid <= 999999):
        raise ValueError(f"invalid CT id: {ctid}")
    return ctid


class BaseRunner:
    """Executes host-level commands (pct ...)."""

    def _wrap(self, argv: list[str]) -> list[str]:
        raise NotImplementedError

    async def run(self, argv: list[str], timeout: int = 300) -> CmdResult:
        full = self._wrap(argv)
        proc = await asyncio.create_subprocess_exec(
            *full,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            return CmdResult(full, 124, "", f"timeout after {timeout}s")
        return CmdResult(full, proc.returncode or 0,
                         out.decode(errors="replace"), err.decode(errors="replace"))

    async def stream(self, argv: list[str],
                     on_line: Callable[[str], None] | None = None,
                     timeout: int = 1800) -> CmdResult:
        """Run a command and yield merged stdout/stderr line by line."""
        full = self._wrap(argv)
        proc = await asyncio.create_subprocess_exec(
            *full,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        lines: list[str] = []

        async def _read() -> None:
            assert proc.stdout is not None
            async for raw in proc.stdout:
                line = raw.decode(errors="replace").rstrip("\n")
                lines.append(line)
                if on_line:
                    on_line(line)

        try:
            await asyncio.wait_for(_read(), timeout=timeout)
            await proc.wait()
        except asyncio.TimeoutError:
            proc.kill()
            return CmdResult(full, 124, "\n".join(lines), "timeout")
        return CmdResult(full, proc.returncode or 0, "\n".join(lines), "")


class LocalRunner(BaseRunner):
    def _wrap(self, argv: list[str]) -> list[str]:
        return argv


class SSHRunner(BaseRunner):
    def __init__(self, host: str, user: str = "root",
                 port: int = 22, key_file: str | None = None,
                 known_hosts: str | None = None):
        self.base = ["ssh", "-o", "BatchMode=yes",
                     "-o", "StrictHostKeyChecking=accept-new"]
        if known_hosts:
            # keep ssh from trying to write /root/.ssh/known_hosts (read-only)
            self.base += ["-o", f"UserKnownHostsFile={known_hosts}"]
        self.base += ["-p", str(port)]
        if key_file:
            self.base += ["-i", key_file]
        self.base.append(f"{user}@{host}")

    def _wrap(self, argv: list[str]) -> list[str]:
        # ssh needs a single remote command string – quote each token safely
        return self.base + [" ".join(shlex.quote(a) for a in argv)]


class Proxmox:
    """High-level pct operations."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        mode = cfg.proxmox.get("mode", "local")
        if mode == "ssh":
            s = cfg.proxmox.get("ssh", {})
            try:
                os.makedirs(_STATE_DIR, exist_ok=True)
            except OSError:
                pass
            self.runner: BaseRunner = SSHRunner(
                host=s.get("host"), user=s.get("user", "root"),
                port=int(s.get("port", 22)), key_file=s.get("key_file"),
                known_hosts=os.path.join(_STATE_DIR, "known_hosts"),
            )
        else:
            self.runner = LocalRunner()

    # ---------- inventory -------------------------------------------------

    async def list_containers(self) -> list[dict]:
        res = await self.runner.run(["pct", "list"])
        containers = []
        if res.ok:
            for line in res.stdout.splitlines()[1:]:
                parts = line.split()
                if not parts or not parts[0].isdigit():
                    continue
                ctid = int(parts[0])
                status = parts[1] if len(parts) > 1 else "unknown"
                name = parts[-1]
                containers.append({"ctid": ctid, "status": status, "name": name})
        return containers

    async def status_detail(self, ctid: int) -> dict:
        ctid = _validate_ctid(ctid)
        res = await self.runner.run(["pct", "status", str(ctid), "--verbose"])
        detail: dict = {"ctid": ctid}
        for line in res.stdout.splitlines():
            if ":" in line:
                k, v = line.split(":", 1)
                detail[k.strip()] = v.strip()
        return detail

    # ---------- exec inside container ------------------------------------

    def exec_argv(self, ctid: int, command: str) -> list[str]:
        """Build `pct exec` argv. `command` is a fixed, app-defined string –
        never raw user input."""
        ctid = _validate_ctid(ctid)
        return ["pct", "exec", str(ctid), "--", "bash", "-lc", command]

    async def exec(self, ctid: int, command: str, timeout: int = 300) -> CmdResult:
        return await self.runner.run(self.exec_argv(ctid, command), timeout)

    async def exec_stream(self, ctid: int, command: str,
                          on_line, timeout: int = 1800) -> CmdResult:
        return await self.runner.stream(self.exec_argv(ctid, command), on_line, timeout)

    # ---------- lifecycle --------------------------------------------------

    async def reboot(self, ctid: int) -> CmdResult:
        return await self.runner.run(["pct", "reboot", str(_validate_ctid(ctid))], 180)

    async def snapshot(self, ctid: int, name: str) -> CmdResult:
        if not re.fullmatch(r"[A-Za-z0-9_\-]{1,40}", name):
            raise ValueError("invalid snapshot name")
        return await self.runner.run(
            ["pct", "snapshot", str(_validate_ctid(ctid)), name,
             "--description", "lxc-commander pre-update snapshot"], 600)

    # ---------- update inspection -----------------------------------------

    async def os_release(self, ctid: int) -> str:
        res = await self.exec(ctid, ". /etc/os-release && echo $PRETTY_NAME")
        return res.stdout.strip() if res.ok else "unknown"

    async def uptime(self, ctid: int) -> str:
        res = await self.exec(ctid, "uptime -p")
        return res.stdout.strip() if res.ok else ""

    async def resource_usage(self, ctid: int) -> dict:
        cmd = (
            "awk '/MemTotal/{t=$2}/MemAvailable/{a=$2}END"
            "{printf \"%.1f %.1f\", (t-a)/1048576, t/1048576}' /proc/meminfo; "
            "echo; df -h / | awk 'NR==2{print $3\" \"$2\" \"$5}'; "
            "cat /proc/loadavg | awk '{print $1}'"
        )
        res = await self.exec(ctid, cmd)
        usage = {"mem_used_gb": None, "mem_total_gb": None,
                 "disk_used": None, "disk_total": None, "disk_pct": None,
                 "load1": None}
        if res.ok:
            lines = res.stdout.strip().splitlines()
            try:
                mu, mt = lines[0].split()
                usage["mem_used_gb"], usage["mem_total_gb"] = float(mu), float(mt)
                du, dt, dp = lines[1].split()
                usage.update(disk_used=du, disk_total=dt, disk_pct=dp)
                usage["load1"] = float(lines[2])
            except (IndexError, ValueError):
                pass
        return usage

    async def check_updates(self, ctid: int) -> dict:
        """apt update + list upgradable. Returns counts + package list."""
        upd = await self.exec(ctid, f"{APT_ENV} apt-get update -qq", timeout=600)
        if not upd.ok:
            return {"state": "unknown", "error": upd.stderr[-500:], "packages": []}
        lst = await self.exec(ctid, "apt list --upgradable 2>/dev/null")
        packages, security = [], 0
        for line in lst.stdout.splitlines():
            if "/" not in line or line.startswith("Listing"):
                continue
            pkg = line.split("/")[0]
            is_sec = "-security" in line
            security += int(is_sec)
            packages.append({"name": pkg, "line": line, "security": is_sec})
        return {
            "state": "updates" if packages else "ok",
            "total": len(packages),
            "security": security,
            "packages": packages,
        }
