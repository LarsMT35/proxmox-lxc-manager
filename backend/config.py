"""Configuration loading for LXC Commander."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

import yaml

CONFIG_PATH = os.environ.get(
    "LXC_COMMANDER_CONFIG",
    os.path.join(os.path.dirname(__file__), "..", "config", "containers.yaml"),
)


@dataclass
class ContainerMeta:
    ctid: int
    name: str
    role: str = ""
    criticality: str = "normal"          # critical | normal | low
    group: str | None = None             # exclusive group (e.g. "dns")
    flags: list[str] = field(default_factory=list)
    validate_command: str | None = None

    @property
    def is_critical(self) -> bool:
        return self.criticality == "critical"


class Config:
    def __init__(self, raw: dict[str, Any], path: str = CONFIG_PATH):
        self.raw = raw
        self.path = path
        self.proxmox: dict[str, Any] = raw.get("proxmox", {"mode": "local"})
        self.server: dict[str, Any] = raw.get("server", {})
        self.safety: dict[str, Any] = raw.get("safety", {})
        self.exclusive_groups: dict[str, list[int]] = raw.get("exclusive_groups") or {}
        self.containers: dict[int, ContainerMeta] = {}
        for ctid, meta in (raw.get("containers") or {}).items():
            ctid = int(ctid)
            self.containers[ctid] = ContainerMeta(
                ctid=ctid,
                name=meta.get("name", f"CT {ctid}"),
                role=meta.get("role", ""),
                criticality=meta.get("criticality", "normal"),
                group=meta.get("group"),
                flags=meta.get("flags", []) or [],
                validate_command=meta.get("validate_command"),
            )

    def meta(self, ctid: int) -> ContainerMeta:
        return self.containers.get(
            int(ctid), ContainerMeta(ctid=int(ctid), name=f"CT {ctid}")
        )

    def group_members(self, group: str | None) -> list[int]:
        if not group:
            return []
        return self.exclusive_groups.get(group, [])

    def adopt_container(self, meta: ContainerMeta) -> None:
        """Register a newly discovered container: persist it to the YAML
        config (comments are preserved) and activate it in memory."""
        entry: dict[str, Any] = {"name": meta.name}
        if meta.role:
            entry["role"] = meta.role
        entry["criticality"] = meta.criticality
        if meta.group:
            entry["group"] = meta.group
        if meta.flags:
            entry["flags"] = list(meta.flags)
        if meta.validate_command:
            entry["validate_command"] = meta.validate_command
        _persist_container(self.path, meta.ctid, entry, meta.group)
        self.containers[meta.ctid] = meta
        if meta.group:
            members = self.exclusive_groups.setdefault(meta.group, [])
            if meta.ctid not in members:
                members.append(meta.ctid)


def _persist_container(path: str, ctid: int, entry: dict[str, Any],
                       group: str | None) -> None:
    """Round-trip edit of containers.yaml so existing comments survive."""
    from ruamel.yaml import YAML

    rt = YAML()
    rt.preserve_quotes = True
    with open(path, "r", encoding="utf-8") as fh:
        doc = rt.load(fh) or {}
    if doc.get("containers") is None:
        doc["containers"] = {}
    doc["containers"][int(ctid)] = entry
    if group:
        if doc.get("exclusive_groups") is None:
            doc["exclusive_groups"] = {}
        members = doc["exclusive_groups"].setdefault(group, [])
        if int(ctid) not in members:
            members.append(int(ctid))
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        rt.dump(doc, fh)
    os.replace(tmp, path)


def load_config(path: str = CONFIG_PATH) -> Config:
    with open(path, "r", encoding="utf-8") as fh:
        return Config(yaml.safe_load(fh) or {}, path=path)
