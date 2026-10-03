#!/usr/bin/env python3
"""人设编辑器 — a standalone editor for a DSH agent preset's persona prompt.

It runs entirely outside DeepSeek Harness: no plugin, no page script, no HTTP
interface. It reads and writes the persona text of one preset inside the
profile's `cordis.patch.yml`, which DSH itself hot-reloads.

What it guarantees before touching the file:

* the new text is rendered back into the same YAML literal block, with every
  other byte of the file preserved (comments, `!!js` tags, indentation);
* the file is re-read after rendering and compared against what you typed, and
  the structural markers (`- id:` rows, `plugins:` keys) must be unchanged;
* the previous file is copied into a timestamped backup first, and the write
  itself goes through a temporary file plus an atomic replace.

Nothing is hardcoded to one machine: the exe finds DSH's own `cordis.patch.yml`
by itself (from `DSH_HOME` / `DSH_PROFILE_DIR`, `~/.dsh`, and a bounded search
when neither is set), works out which preset and which persona plugin row to
edit from the file's contents, and keeps its persona files in a `personas`
folder beside itself. `config.json` beside the exe is an optional override.

Double-click the exe for the window, or use the command line:

    人设编辑.exe --list                  # 列出这台机器上找到的补丁文件和 preset
    人设编辑.exe --check                 # 打印当前状态，不改任何东西
    人设编辑.exe --print                 # 把当前人设打到标准输出
    人设编辑.exe --set-file 某个.md       # 用某个人设文件覆盖当前 preset
    人设编辑.exe --revert                # 撤销上一次保存
    人设编辑.exe --preset <preset id>     # 指定要编辑的 preset（默认自动选择）
    人设编辑.exe --patch <文件> --personas <目录>
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import struct
import sys
import time
from collections import Counter
from collections.abc import Iterator
from pathlib import Path

APP_NAME = "人设编辑器"
APP_VERSION = "2.1.2"

# ── 常量 ─────────────────────────────────────────────────────────────────────

PATCH_NAME = "cordis.patch.yml"
CONFIG_NAME = "config.json"
PERSONA_PACKAGE = "dsh-persona"
PERSONA_PACKAGE_NAME = "@deepseek-ai/dsh-persona"
PRESET_PACKAGE = "@deepseek-ai/dsh-agent-preset"
REGISTRY_PACKAGES = ("@deepseek-ai/dsh-agent-preset-registry", "dsh-agent-preset-registry")
PRESET_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
INSERT_KEY_RE = re.compile(r"^\s*-\s+(\S+):\s*$")

# Where the web-app bundle keeps its shipped preset patches, on disk and
# inside the Desktop app's app.asar.
SHIPPED_PRESET_DIRS = (
    "node_modules/@deepseek-ai/dsh-web-app/presets",
    "packages/bundle/web-app/presets",
)
ASAR_PRESET_PREFIX = "dsh/node_modules/@deepseek-ai/dsh-web-app/presets/"

STARTER_PERSONA = (
    "# 角色设定\n"
    "在这里写人设提示词。\n"
    "\n"
    "# 语言与风格\n"
    "全部输出使用简体中文。\n"
)

# ── 路径 ─────────────────────────────────────────────────────────────────────


def app_dir() -> Path:
    """The folder holding the exe (frozen) or this script (source)."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def emit(text: str) -> None:
    """Print when a console exists; otherwise log beside the exe and show a box.

    The windowed build has no stdout at all, and a double-clicked shortcut can
    still pass arguments — so command-line results must remain visible there.
    """
    stream = sys.stdout
    if stream is not None:
        try:
            stream.write(text + ("" if text.endswith("\n") else "\n"))
            stream.flush()
            return
        except Exception:
            pass
    try:
        with (app_dir() / "人设编辑.log").open("a", encoding="utf-8") as handle:
            handle.write(time.strftime("[%Y-%m-%d %H:%M:%S] ") + text + "\n")
    except OSError:
        pass
    try:
        import tkinter as tk
        from tkinter import messagebox

        root = tk.Tk()
        root.withdraw()
        messagebox.showinfo(APP_NAME, text)
        root.destroy()
    except Exception:
        pass


def read_env_path(name: str) -> Path | None:
    """A path from an environment variable, or None when it is unset or empty."""
    raw = os.environ.get(name, "").strip().strip('"')
    return Path(raw) if raw else None


def _candidate_homes() -> list[Path]:
    """DSH homes this machine may use, most likely first."""
    homes: list[Path] = []

    def add(candidate: Path | None) -> None:
        if candidate is None:
            return
        try:
            resolved = candidate.expanduser().resolve()
        except (OSError, RuntimeError):
            return
        if resolved not in homes:
            homes.append(resolved)

    add(read_env_path("DSH_HOME"))
    profile_dir = read_env_path("DSH_PROFILE_DIR")
    if profile_dir is not None:  # a profile is <home>/profiles/<name>
        add(profile_dir.parent.parent)
        add(profile_dir.parent)
    add(Path.home() / ".dsh")
    add(app_dir() / ".dsh")
    add(app_dir())  # the exe copied into the home itself
    return homes


# Directories that never hold a DSH home. Skipping them keeps the fallback
# search short on a machine with several large drives.
SKIP_DIR_NAMES = {
    "$recycle.bin", ".cache", ".git", "appdata", "application data", "cache",
    "code cache", "config.msi", "gpu cache", "logs", "node_modules",
    "program files", "program files (x86)", "programdata", "recovery",
    "system volume information", "windows",
}


def _scan_for_homes(limit: int = 4000) -> list[Path]:
    """Look for a DSH home (a directory holding `profiles/`) by itself.

    Bounded on purpose: the walk visits at most `limit` directories, two levels
    below the user profile and below each drive root, so this can never turn
    into a whole-disk search on someone else's machine.
    """
    roots = [
        Path.home(),
        Path.home() / "AppData" / "Local",
        Path.home() / "AppData" / "Roaming",
    ]
    for letter in "CDEFGHIJKLMNOPQRSTUVWXYZ":
        drive = Path(f"{letter}:\\")
        try:
            if drive.exists():
                roots.append(drive)
        except OSError:
            continue

    found: list[Path] = []
    visited = 0

    def walk(parent: Path, depth: int) -> None:
        nonlocal visited
        if depth < 0 or visited > limit:
            return
        try:
            entries = list(parent.iterdir())
        except OSError:
            return
        for entry in entries:
            if visited > limit:
                return
            visited += 1
            try:
                if not entry.is_dir() or entry.name.lower() in SKIP_DIR_NAMES:
                    continue
                if (entry / "profiles").is_dir():
                    found.append(entry.resolve())
                    continue
            except OSError:
                continue
            walk(entry, depth - 1)

    for root in roots:
        walk(root, 1)
    return found


def dsh_homes() -> list[Path]:
    """Existing DSH homes, most likely first."""
    homes = _candidate_homes()
    usable = [home for home in homes if (home / "profiles").is_dir()]
    if usable:
        return usable
    for found in _scan_for_homes():
        if found not in homes:
            homes.append(found)
    return [home for home in homes if (home / "profiles").is_dir()] or homes[:1]


def dsh_home() -> Path:
    """The DSH home to report and to fall back on."""
    homes = dsh_homes()
    return homes[0] if homes else Path.home() / ".dsh"


def _bundle_patches(modules: Path) -> list[Path]:
    """Patches of bundles installed in a profile that declare a preset."""
    patches: list[Path] = []
    for pattern in ("*/cordis.patch.yml", "@*/*/cordis.patch.yml"):
        for candidate in sorted(modules.glob(pattern)):
            try:
                if not candidate.is_file():
                    continue
                if PRESET_PACKAGE in candidate.read_text(encoding="utf-8", errors="replace"):
                    patches.append(candidate)
            except OSError:
                continue
    return patches


def summarize_patch(path: Path) -> dict:
    """Parse one patch file: its presets and the preset DSH has selected."""
    try:
        lines = split_text(Path(path).read_text(encoding="utf-8", errors="replace"))["lines"]
    except OSError as error:
        return {"presets": [], "selected_default": None, "error": str(error)}
    presets = list_presets(lines)
    for preset in presets:
        rows = preset_persona_rows(lines, preset)
        lengths = []
        for row in rows:
            field = read_scalar(lines, row, persona_field_name(lines, row) or "prefix")
            lengths.append(len(field.get("text", "")))
        preset["persona_rows"] = [row["id"] for row in rows]
        preset["persona_length"] = max(lengths, default=0)
    return {"presets": presets, "selected_default": selected_default_preset(lines), "error": None}


def rank_patch_files(entries: list[dict], current: str = "") -> list[dict]:
    """Summarize and order candidates: current profile, presets, then path."""
    ranked = [{**entry, **summarize_patch(entry["path"])} for entry in entries]

    def sort_key(entry: dict) -> tuple:
        presets = entry.get("presets") or []
        editable = sum(1 for preset in presets if preset.get("persona_length"))
        return (
            0 if current and entry["profile"] == current else 1,
            0 if entry["source"] == "当前 profile" else (1 if entry["source"] == "profile 补丁" else 2),
            -editable,
            -len(presets),
            entry["profile"],
            str(entry["path"]).lower(),
        )

    return sorted(ranked, key=sort_key)


def discover_patch_files() -> list[dict]:
    """Every patch file on this machine that can hold an editable preset."""
    current = os.environ.get("DSH_PROFILE", "").strip()
    profile_dir = read_env_path("DSH_PROFILE_DIR")
    entries: list[dict] = []
    seen: set[str] = set()

    def add(path: Path, profile: str, home: Path | None, source: str) -> None:
        try:
            if not path.is_file():
                return
            key = str(path.resolve()).lower()
        except (OSError, RuntimeError):
            return
        if key in seen:
            return
        seen.add(key)
        entries.append({"path": path, "profile": profile, "home": home, "source": source})

    if profile_dir is not None:
        add(profile_dir / PATCH_NAME, current or profile_dir.name, profile_dir.parent.parent, "当前 profile")
    for home in dsh_homes():
        profiles = home / "profiles"
        if not profiles.is_dir():
            continue
        names = sorted((entry for entry in profiles.iterdir() if entry.is_dir()), key=lambda item: item.name)
        for entry in names:
            add(entry / PATCH_NAME, entry.name, home, "profile 补丁")
        for entry in names:
            modules = entry / "node_modules"
            if modules.is_dir():
                for patch in _bundle_patches(modules):
                    add(patch, entry.name, home, "插件包补丁")
    return rank_patch_files(entries, current)


def default_patch_file() -> Path:
    """The patch file to edit when nothing else was configured."""
    discovered = discover_patch_files()
    if discovered:
        return discovered[0]["path"]
    profile_dir = read_env_path("DSH_PROFILE_DIR")
    if profile_dir is not None:
        return profile_dir / PATCH_NAME
    return dsh_home() / "profiles" / "desktop" / PATCH_NAME


def _writable(directory: Path) -> bool:
    """Can this tool create/keep files here? Probes without leaving anything.

    A probe file rather than `os.access`, because on Windows `os.access` reports
    Program Files as writable (it only reads the read-only attribute).
    """
    probe_dir = directory if directory.is_dir() else directory.parent
    if not probe_dir.is_dir():
        return False
    probe = probe_dir / ".persona-editor-write-test"
    try:
        probe.write_text("", encoding="utf-8")
        probe.unlink()
        return True
    except OSError:
        return False


def default_personas_dir() -> Path:
    """Portable by default: a `personas` folder beside the exe.

    A read-only home (Program Files, a locked share) falls back to the DSH home
    so the editor still has somewhere to keep persona files and backups.
    """
    beside = app_dir() / "personas"
    if _writable(beside):
        return beside
    fallback = dsh_home() / "personas"
    if _writable(fallback):
        return fallback
    return beside


def load_config() -> dict:
    """Optional config.json beside the exe; it is a machine-local override."""
    config_path = app_dir() / CONFIG_NAME
    if not config_path.exists():
        return {}
    try:
        data = json.loads(config_path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_config(updates: dict) -> Path | None:
    """Merge a machine-local choice into config.json beside the exe."""
    config_path = app_dir() / CONFIG_NAME
    data = load_config()
    data.update(updates)
    try:
        config_path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except OSError:
        return None
    return config_path


def config_text(config: dict, key: str) -> str | None:
    value = config.get(key)
    return value.strip() if isinstance(value, str) and value.strip() else None


def config_path(config: dict, key: str, must_exist: bool = False) -> Path | None:
    """A path from config.json, but only when it makes sense on this machine.

    A config.json copied from someone else's folder names paths this machine
    does not have. Ignoring them keeps discovery (or a CLI flag) in charge
    instead of failing, or creating a stray directory on another drive.
    """
    value = config_text(config, key)
    if value is None:
        return None
    path = Path(value)
    if must_exist and not path.is_file():
        return None
    if not must_exist and not (path.is_dir() or path.parent.is_dir()):
        return None
    return path


class Target:
    """Resolved paths plus the preset and persona row this tool edits.

    `preset_id`, `preset_row_id` and `persona_row_id` are preferences, never
    requirements. Any of them may be None, and a value that does not match this
    machine falls back to auto-detection, so a config.json copied from someone
    else's folder cannot make the editor fail.
    """

    def __init__(
        self,
        patch_file: Path,
        personas_dir: Path,
        preset_id: str | None = None,
        preset_row_id: str | None = None,
        persona_row_id: str | None = None,
    ) -> None:
        self.patch_file = Path(patch_file)
        self.personas_dir = Path(personas_dir)
        self.backups_dir = self.personas_dir / "_backups"
        self.preset_id = preset_id
        self.preset_row_id = preset_row_id
        self.persona_row_id = persona_row_id

    @classmethod
    def resolve(cls, args: argparse.Namespace) -> "Target":
        config = load_config()
        patch = (
            getattr(args, "patch", None)
            or config_path(config, "patchFile", must_exist=True)
            or default_patch_file()
        )
        personas = (
            getattr(args, "personas", None)
            or config_path(config, "personasDir")
            or default_personas_dir()
        )
        target = cls(
            patch_file=Path(patch),
            personas_dir=Path(personas),
            preset_id=getattr(args, "preset", None) or config_text(config, "presetId") or None,
            preset_row_id=config_text(config, "presetRowId") or None,
            persona_row_id=getattr(args, "persona_row", None) or config_text(config, "personaRowId") or None,
        )
        return target


# ── YAML 行级编辑（与 DSH 侧插件同一套规则） ─────────────────────────────────

ROW_RE = re.compile(r"^(\s*)-\s+id:\s*(\S+)\s*$")
BLOCK_HEADER_RE = re.compile(r"^[|>][+-]?\d*$")


def indent_of(line: str) -> int:
    return len(line) - len(line.lstrip(" \t"))


def is_blank(line: str) -> bool:
    return line.strip() == ""


def unquote(value: str) -> str:
    text = value.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "'\"":
        return text[1:-1]
    return text


def split_text(raw: str) -> dict:
    bom = raw.startswith("\ufeff")
    body = raw[1:] if bom else raw
    eol = "\r\n" if "\r\n" in body else "\n"
    return {"bom": bom, "eol": eol, "lines": re.split(r"\r\n|\n", body)}


def join_text(parts: dict) -> str:
    return ("\ufeff" if parts["bom"] else "") + parts["eol"].join(parts["lines"])


def block_end(lines: list[str], index: int, indent: int) -> int:
    cursor = index + 1
    while cursor < len(lines):
        if not is_blank(lines[cursor]) and indent_of(lines[cursor]) <= indent:
            break
        cursor += 1
    return cursor


def rows_within(lines: list[str], start: int, end: int, indent: int) -> list[dict]:
    rows: list[dict] = []
    cursor = start
    while cursor < end:
        match = ROW_RE.match(lines[cursor])
        if match and len(match.group(1)) == indent:
            rows.append({"id": match.group(2), "index": cursor, "end": block_end(lines, cursor, indent)})
        cursor += 1
    return rows


def list_presets(lines: list[str]) -> list[dict]:
    """Every row whose block declares `plugins:` — that is a preset row."""
    presets: list[dict] = []
    index = 0
    while index < len(lines):
        match = ROW_RE.match(lines[index])
        if match is None:
            index += 1
            continue
        row_indent = len(match.group(1))
        end = block_end(lines, index, row_indent)
        plugins_key = -1
        config_key = -1
        for cursor in range(index + 1, end):
            if is_blank(lines[cursor]):
                continue
            if indent_of(lines[cursor]) == row_indent + 2 and re.match(r"^config:\s*$", lines[cursor]):
                config_key = cursor
            if re.match(r"^\s*plugins:\s*$", lines[cursor]):
                plugins_key = cursor
        if plugins_key == -1:
            index = end
            continue

        field_indent = row_indent + 4 if config_key == -1 else indent_of(lines[config_key]) + 2
        preset_id = None
        name = None
        description = None
        order = None
        for cursor in range(index + 1, end):
            if is_blank(lines[cursor]) or indent_of(lines[cursor]) != field_indent:
                continue
            for key, pattern in (
                ("id", r"^\s*id:\s*(.+?)\s*$"),
                ("name", r"^\s*name:\s*(.+?)\s*$"),
                ("description", r"^\s*description:\s*(.+?)\s*$"),
                ("order", r"^\s*order:\s*(.+?)\s*$"),
            ):
                found = re.match(pattern, lines[cursor])
                if found is None:
                    continue
                if key == "id" and preset_id is None:
                    preset_id = unquote(found.group(1))
                elif key == "name" and name is None:
                    name = unquote(found.group(1))
                elif key == "description" and description is None:
                    description = unquote(found.group(1))
                elif key == "order" and order is None:
                    order = unquote(found.group(1))

        presets.append(
            {
                "row_id": match.group(2),
                "preset_id": preset_id,
                "name": name,
                "description": description,
                "order": order,
                "start": index,
                "end": end,
                "plugins_key": plugins_key,
                "plugins_indent": indent_of(lines[plugins_key]),
            }
        )
        index = end
    return presets


def preset_label(preset: dict) -> str:
    return preset.get("preset_id") or preset.get("row_id") or "?"


def preset_has_persona(lines: list[str], preset: dict) -> bool:
    return bool(preset_persona_rows(lines, preset))


def resolve_preset(lines: list[str], target: Target) -> dict | None:
    """Pick the preset to edit, preferring what the user/Dsh already chose.

    Every configured value is a preference rather than a requirement, so a
    config.json from another machine (or a preset that was renamed) degrades to
    auto-detection instead of an error.
    """
    presets = list_presets(lines)
    if not presets:
        return None

    wanted = (target.preset_id or "").strip()
    wanted_row = (target.preset_row_id or "").strip()
    if wanted or wanted_row:
        for preset in presets:
            if wanted and preset["preset_id"] == wanted:
                preset["resolved_by"] = "配置里指定的 preset"
                return preset
        for preset in presets:
            if wanted and preset["row_id"] == f"preset-{wanted}":
                preset["resolved_by"] = "配置里指定的 preset"
                return preset
        for preset in presets:
            if wanted_row and preset["row_id"] == wanted_row:
                preset["resolved_by"] = "配置里指定的预设行"
                return preset

    active = selected_default_preset(lines)
    if active:
        for preset in presets:
            if preset["preset_id"] == active and preset_has_persona(lines, preset):
                preset["resolved_by"] = f"DSH 当前选中的 preset（{active}）"
                return preset
    for preset in presets:
        if preset_has_persona(lines, preset):
            preset["resolved_by"] = "文件里第一个带 persona 的 preset"
            return preset
    if active:
        for preset in presets:
            if preset["preset_id"] == active:
                preset["resolved_by"] = f"DSH 当前选中的 preset（{active}）"
                return preset
    presets[0]["resolved_by"] = "文件里第一个 preset"
    return presets[0]


def read_scalar(lines: list[str], row: dict, key: str) -> dict:
    pattern = re.compile(rf"^(\s*){key}:\s*(.*)$")
    for cursor in range(row["index"] + 1, row["end"]):
        match = pattern.match(lines[cursor])
        if match is None:
            continue
        field_indent = len(match.group(1))
        raw = match.group(2).strip()
        if raw == "":
            return {"found": True, "field_indent": field_indent, "style": None, "text": "",
                    "key_index": cursor, "content_start": cursor + 1, "content_end": cursor + 1}
        if not BLOCK_HEADER_RE.match(raw):
            return {"found": True, "field_indent": field_indent, "style": "inline", "text": unquote(raw),
                    "key_index": cursor, "content_start": cursor + 1, "content_end": cursor + 1}
        content_end = cursor + 1
        while content_end < row["end"]:
            if not is_blank(lines[content_end]) and indent_of(lines[content_end]) <= field_indent:
                break
            content_end += 1
        content = lines[cursor + 1: content_end]
        while content and is_blank(content[0]):
            content.pop(0)
        while content and is_blank(content[-1]):
            content.pop()
        widths = [indent_of(line) for line in content if not is_blank(line)]
        content_indent = min(widths) if widths else 0
        text = "\n".join("" if is_blank(line) else line[content_indent:] for line in content)
        strip = raw.startswith("|") or raw.startswith(">")
        if content and not (strip and "-" in raw):
            text += "\n"
        return {"found": True, "field_indent": field_indent, "style": raw, "text": text,
                "key_index": cursor, "content_start": cursor + 1, "content_end": content_end}
    return {"found": False}


# ── 认行：preset、注册表、persona ────────────────────────────────────────────


def scalar_text(lines: list[str], row: dict, key: str) -> str:
    """The inline text of a key inside a row, or "" when it is absent."""
    field = read_scalar(lines, row, key)
    return str(field.get("text", "")) if field.get("found") else ""


def top_level_rows(lines: list[str]) -> list[dict]:
    """Rows that start a block of their own, at whatever indent they sit."""
    rows: list[dict] = []
    index = 0
    while index < len(lines):
        match = ROW_RE.match(lines[index])
        if match is None:
            index += 1
            continue
        indent = len(match.group(1))
        end = block_end(lines, index, indent)
        rows.append({"id": match.group(2), "index": index, "end": end, "indent": indent})
        index = end
    return rows


def selected_default_preset(lines: list[str]) -> str | None:
    """`selectedDefault` from the agent-preset registry row, when it has one."""
    for row in top_level_rows(lines):
        name = scalar_text(lines, row, "name")
        if not (row["id"].startswith("agent-preset-registry") or name in REGISTRY_PACKAGES):
            continue
        selected = scalar_text(lines, row, "selectedDefault")
        if selected:
            return selected
    return None


def plugin_row_name(lines: list[str], row: dict) -> str:
    return scalar_text(lines, row, "name").strip()


def persona_field_name(lines: list[str], row: dict) -> str | None:
    """Which key carries this persona: `prefix` today, `text` in old files."""
    for key in ("prefix", "text"):
        if read_scalar(lines, row, key)["found"]:
            return key
    return None


def is_persona_row(lines: list[str], row: dict) -> bool:
    """True for the plugin row that carries a preset's persona prompt."""
    name = plugin_row_name(lines, row)
    if PERSONA_PACKAGE in name:
        return True
    if "persona" in row["id"].lower():
        return True
    # A row this tool cannot name but that carries persona text is still a
    # persona row: that is how the first version of this tool found it.
    return not name and persona_field_name(lines, row) is not None


def preset_persona_rows(lines: list[str], preset: dict) -> list[dict]:
    rows = rows_within(lines, preset["plugins_key"] + 1, preset["end"], preset["plugins_indent"] + 2)
    return [row for row in rows if is_persona_row(lines, row)]


def find_persona_row(lines: list[str], preset: dict, hint: str | None = None) -> tuple[dict | None, str]:
    """The persona plugin row of a preset, plus how it was identified."""
    rows = rows_within(lines, preset["plugins_key"] + 1, preset["end"], preset["plugins_indent"] + 2)
    if hint:
        for row in rows:
            if row["id"] == hint:
                return row, "按配置指定"
    candidates = [row for row in rows if is_persona_row(lines, row)]
    if not candidates:
        return None, ""
    with_text = [row for row in candidates if persona_field_name(lines, row)]
    pool = with_text or candidates
    chosen = next((row for row in pool if row["id"] == "persona"), pool[0])
    reason = "按包名识别" if plugin_row_name(lines, chosen) else "按字段识别"
    return chosen, reason


def canonical_text(text: str) -> str:
    """The one form a literal block can hold: no leading/trailing blank lines,
    no trailing spaces, LF endings."""
    lines = str(text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    while lines and lines[-1].strip() == "":
        lines.pop()
    while lines and lines[0].strip() == "":
        lines.pop(0)
    return "\n".join(line.rstrip(" \t") for line in lines)


def render_scalar(field_indent: int, key: str, text: str) -> list[str]:
    lines = canonical_text(text).split("\n")
    pad = " " * field_indent
    if lines == [""]:
        return [f"{pad}{key}: ''"]
    rendered = [f"{pad}{key}: |-"]
    for line in lines:
        rendered.append("" if is_blank(line) else " " * (field_indent + 2) + line)
    return rendered


def inspect_persona(lines: list[str], target: Target) -> dict:
    """Everything about the persona this target resolves to, or a clear error."""
    preset = resolve_preset(lines, target)
    if preset is None:
        raise LookupError(f"这个补丁文件里还没有任何 preset：{target.patch_file}")
    label = preset_label(preset)
    row, reason = find_persona_row(lines, preset, target.persona_row_id)
    if row is None:
        raise LookupError(
            f"preset「{label}」里没有 persona 插件行；本工具只改人设文本，不会新建预设结构"
        )
    field_name = persona_field_name(lines, row) or "prefix"
    field = read_scalar(lines, row, field_name)
    return {
        "preset": preset,
        "preset_label": label,
        "resolved_by": preset.get("resolved_by", ""),
        "row": row,
        "row_id": row["id"],
        "row_reason": reason,
        "package": plugin_row_name(lines, row),
        "field": field_name,
        "text": str(field.get("text", "")) if field.get("found") else "",
    }


def read_persona(lines: list[str], target: Target) -> str:
    return inspect_persona(lines, target)["text"]


def replace_persona(lines: list[str], target: Target, text: str) -> tuple[list[str], dict]:
    info = inspect_persona(lines, target)
    row = info["row"]
    field = read_scalar(lines, row, info["field"])
    report = {
        "preset_label": info["preset_label"],
        "resolved_by": info["resolved_by"],
        "row_id": info["row_id"],
        "package": info["package"],
        "field": info["field"],
    }

    if field.get("found"):
        report["previous_length"] = len(info["text"])
        report["style"] = "inline → |-" if field["style"] == "inline" else field["style"]
        # The file's own trailing blank lines live inside the block range when
        # the persona block is the last thing in the file, so keep them: a
        # write must never eat the file's final newline.
        tail = lines[field["content_end"]:]
        trailing: list[str] = []
        if not tail:
            consumed = lines[field["key_index"] + 1: field["content_end"]]
            while consumed and is_blank(consumed[-1]):
                trailing.insert(0, consumed.pop())
        rendered = render_scalar(field["field_indent"], info["field"], text) + trailing
        return lines[: field["key_index"]] + rendered + tail, report

    # The persona row exists but carries no text yet: insert the key it uses.
    preset = info["preset"]
    config_index = -1
    for cursor in range(row["index"] + 1, row["end"]):
        if re.match(r"^\s*config:\s*$", lines[cursor]):
            config_index = cursor
            break
    if config_index == -1:
        field_indent = preset["plugins_indent"] + 6
        insert_at = row["index"] + 1
        replacement = [" " * (preset["plugins_indent"] + 4) + "config:"] + render_scalar(field_indent, info["field"], text)
    else:
        insert_at = config_index + 1
        replacement = render_scalar(indent_of(lines[config_index]) + 2, info["field"], text)
    report["previous_length"] = 0
    report["style"] = f"插入 {info['field']}"
    return lines[:insert_at] + replacement + lines[insert_at:], report


def structure_markers(lines: list[str]) -> tuple[int, int]:
    rows = sum(1 for line in lines if ROW_RE.match(line))
    plugins = sum(1 for line in lines if re.match(r"^\s*plugins:\s*$", line))
    return rows, plugins


# ── 读写与备份 ───────────────────────────────────────────────────────────────


def list_persona_files(directory: Path) -> list[dict]:
    if not directory.is_dir():
        return []
    files = []
    for entry in sorted(directory.iterdir(), key=lambda item: item.name):
        if not entry.is_file() or entry.suffix.lower() not in {".md", ".markdown", ".txt", ".text", ".persona"}:
            continue
        try:
            text = entry.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        preview = next((line.strip() for line in text.splitlines() if line.strip()), "")
        files.append({"name": entry.name, "path": entry, "bytes": entry.stat().st_size, "preview": preview[:80]})
    return files


def backup_patch(patch_file: Path, backups_dir: Path) -> Path:
    backups_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S") + f"{int(time.time() * 1000) % 1000:03d}"
    target = backups_dir / f"{patch_file.name}.{stamp}.bak"
    target.write_bytes(patch_file.read_bytes())
    try:
        existing = sorted(
            entry for entry in backups_dir.iterdir()
            if entry.name.startswith(f"{patch_file.name}.") and entry.name.endswith(".bak")
        )
        for stale in existing[:-40]:
            stale.unlink(missing_ok=True)
    except OSError:
        pass
    return target


def newest_backup(patch_file: Path, backups_dir: Path) -> Path | None:
    if not backups_dir.is_dir():
        return None
    entries = sorted(
        entry for entry in backups_dir.iterdir()
        if entry.name.startswith(f"{patch_file.name}.") and entry.name.endswith(".bak")
    )
    return entries[-1] if entries else None


def atomic_write(path: Path, text: str) -> None:
    temporary = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    temporary.write_text(text, encoding="utf-8", newline="")
    os.replace(temporary, path)


def atomic_write_bytes(path: Path, data: bytes) -> None:
    temporary = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    temporary.write_bytes(data)
    os.replace(temporary, path)


def read_text_exact(path: Path) -> str:
    """Read text without translating line endings.

    `read_text` applies universal newlines, which turns a CRLF patch file into
    LF as soon as anything is written back. A user's file may well come from
    Notepad, so its line endings are part of what this tool promises to keep.
    """
    with path.open("r", encoding="utf-8", newline="") as handle:
        return handle.read()


def write_persona(target: Target, text: str) -> dict:
    """Render, verify, back up, then replace — the same order the DSH plugin uses."""
    if not target.patch_file.is_file():
        raise FileNotFoundError(f"补丁文件不存在：{target.patch_file}")
    raw = read_text_exact(target.patch_file)
    parts = split_text(raw)
    wanted = canonical_text(text)
    new_lines, report = replace_persona(parts["lines"], target, wanted)

    rendered = join_text({**parts, "lines": new_lines})

    # Verification 1: reading the result back must return exactly the input.
    read_back = read_persona(split_text(rendered)["lines"], target)
    if read_back != wanted:
        raise ValueError("写回后重新读取的人设文本与输入不一致，已放弃写入")
    # Verification 2: the document's structure must be unchanged.
    if structure_markers(new_lines) != structure_markers(parts["lines"]):
        raise ValueError("补丁文件的结构标记（- id 行 / plugins 键）发生了变化，已放弃写入")

    backup = backup_patch(target.patch_file, target.backups_dir)
    try:
        atomic_write(target.patch_file, rendered)
    except OSError as error:
        try:
            target.patch_file.write_bytes(backup.read_bytes())
        except OSError:
            pass
        raise RuntimeError(f"写入失败：{error}") from error

    return {
        "ok": True,
        "previous_length": report["previous_length"],
        "length": len(wanted),
        "style": report["style"],
        "normalized": wanted != str(text or ""),
        "backup": backup,
        "patch_file": target.patch_file,
        "preset_label": report.get("preset_label"),
        "resolved_by": report.get("resolved_by"),
        "row_id": report.get("row_id"),
        "field": report.get("field"),
    }


def revert_patch(target: Target) -> dict:
    """Pop one save: restore the newest backup and consume it.

    Consuming matters. The first version re-backed-up the current file before
    restoring, so the newest backup became "the state you are leaving" and every
    later revert traded the same two states back and forth. Dropping the used
    backup makes repeated reverts walk back through the save history instead.
    """
    if not target.patch_file.is_file():
        raise FileNotFoundError(f"补丁文件不存在：{target.patch_file}")
    source = newest_backup(target.patch_file, target.backups_dir)
    if source is None:
        raise FileNotFoundError("没有可用备份")
    restored = read_text_exact(source)
    current = read_text_exact(target.patch_file)
    parts = split_text(current)
    new_lines, _ = replace_persona(parts["lines"], target, read_persona(split_text(restored)["lines"], target))
    atomic_write(target.patch_file, join_text({**parts, "lines": new_lines}))
    try:
        source.unlink()
    except OSError:
        pass
    return {"ok": True, "restored_from": source}


# ── 预设模板：本机现成的 preset + DSH 自带的那几个 ────────────────────────────


def yaml_scalar(value: str) -> str:
    """Render a string as a safe single-line YAML scalar."""
    text = str(value)
    if text and re.fullmatch(r"[A-Za-z0-9_./@\-+ ]+", text) and not text.startswith(("-", " ")):
        return text
    return "'" + text.replace("'", "''") + "'"


def indent_block(lines: list[str], target_indent: int) -> list[str]:
    """Re-indent a whole block so its first non-blank line sits at target_indent."""
    first = next((line for line in lines if line.strip()), "")
    delta = target_indent - indent_of(first)
    if delta == 0:
        return list(lines)
    if delta > 0:
        pad = " " * delta
        return [pad + line if line.strip() else line for line in lines]
    out = []
    for line in lines:
        if not line.strip():
            out.append(line)
            continue
        leading = len(line) - len(line.lstrip(" \t"))
        out.append(line[min(-delta, leading):])
    return out


def block_without_trailing_blanks(lines: list[str]) -> list[str]:
    body = list(lines)
    while body and is_blank(body[-1]):
        body.pop()
    return body


def template_block_lines(lines: list[str], preset: dict) -> list[str]:
    """A preset's own declaration, normalised into one `- insert:` entry."""
    body = block_without_trailing_blanks(lines[preset["start"]:preset["end"]])
    return ["- insert:"] + indent_block(body, 4)


def _sole_preset(lines: list[str]) -> dict:
    presets = list_presets(lines)
    if not presets:
        raise ValueError("模板里没有可用的 preset 声明")
    return presets[0]


def set_config_scalar(lines: list[str], key: str, value: str) -> list[str]:
    """Set, or insert just before `plugins:`, a scalar directly under config."""
    preset = _sole_preset(lines)
    indent = preset["plugins_indent"]
    pattern = re.compile(rf"^(\s*){re.escape(key)}:\s*(.*)$")
    for cursor in range(preset["start"] + 1, preset["end"]):
        match = pattern.match(lines[cursor])
        if match and len(match.group(1)) == indent:
            return lines[:cursor] + [" " * indent + f"{key}: {value}"] + lines[cursor + 1:]
    return lines[:preset["plugins_key"]] + [" " * indent + f"{key}: {value}"] + lines[preset["plugins_key"]:]


def insert_plugin_row(lines: list[str], row: list[str]) -> list[str]:
    """Append a plugin row to the preset's plugin list."""
    preset = _sole_preset(lines)
    cursor = preset["end"]
    while cursor > preset["start"] and is_blank(lines[cursor - 1]):
        cursor -= 1
    return lines[:cursor] + row + lines[cursor:]


def ensure_persona_row(lines: list[str]) -> list[str]:
    """Give the template a persona plugin row when it has none."""
    preset = _sole_preset(lines)
    if find_persona_row(lines, preset)[0] is not None:
        return lines
    row_indent = preset["plugins_indent"] + 2
    return insert_plugin_row(
        lines,
        [
            " " * row_indent + "- id: persona",
            " " * (row_indent + 2) + f"name: {yaml_scalar(PERSONA_PACKAGE_NAME)}",
        ],
    )


def iter_dirs(root: Path, depth: int, budget: list[int] | None = None, limit: int = 3000) -> Iterator[Path]:
    """Directories up to `depth` levels below root, skipping noisy names.

    `budget` is a shared counter so several roots searched in one run cannot
    add up to a full-disk walk.
    """
    spent = budget if budget is not None else [0]
    stack: list[tuple[Path, int]] = [(root, 0)]
    while stack:
        parent, level = stack.pop()
        try:
            entries = list(parent.iterdir())
        except OSError:
            continue
        for entry in entries:
            if spent[0] > limit:
                return
            spent[0] += 1
            try:
                if not entry.is_dir() or entry.name.lower() in SKIP_DIR_NAMES:
                    continue
            except OSError:
                continue
            yield entry
            if level < depth:
                stack.append((entry, level + 1))


def running_app_asars() -> list[Path]:
    """`resources/app.asar` of every running process on this machine.

    The Desktop app tells us exactly where it is installed, which beats
    guessing install directories. Only same-user processes can be queried;
    everything is wrapped so a failure here is never fatal.
    """
    if os.name != "nt":
        return []
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        psapi = ctypes.WinDLL("psapi", use_last_error=True)
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel32.QueryFullProcessImageNameW.argtypes = (
            wintypes.HANDLE,
            wintypes.DWORD,
            wintypes.LPWSTR,
            ctypes.POINTER(wintypes.DWORD),
        )
        psapi.EnumProcesses.argtypes = (ctypes.POINTER(wintypes.DWORD), wintypes.DWORD, ctypes.POINTER(wintypes.DWORD))

        capacity = 8192
        pids = (wintypes.DWORD * capacity)()
        needed = wintypes.DWORD()
        if not psapi.EnumProcesses(pids, ctypes.sizeof(pids), ctypes.byref(needed)):
            return []
        found: list[Path] = []
        for index in range(needed.value // ctypes.sizeof(wintypes.DWORD)):
            pid = pids[index]
            if not pid:
                continue
            handle = kernel32.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED_INFORMATION
            if not handle:
                continue
            try:
                size = wintypes.DWORD(32768)
                buffer = ctypes.create_unicode_buffer(size.value)
                if kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
                    candidate = Path(buffer.value).parent / "resources" / "app.asar"
                    if candidate.is_file() and candidate not in found:
                        found.append(candidate)
            finally:
                kernel32.CloseHandle(handle)
        return found
    except Exception:  # noqa: BLE001 - discovery must never break the tool
        return []


def install_search_roots() -> list[Path]:
    """Places a dsh installation (or its app directory) may live."""
    roots: list[Path] = []

    def add(candidate: Path | None) -> None:
        if candidate is None:
            return
        try:
            resolved = candidate.expanduser().resolve()
        except (OSError, RuntimeError):
            return
        if resolved.is_dir() and resolved not in roots:
            roots.append(resolved)

    add(app_dir())
    add(dsh_home())
    add(Path.home())
    add(Path.home() / "AppData" / "Local" / "Programs")
    for name in ("LOCALAPPDATA", "APPDATA", "ProgramFiles", "ProgramFiles(x86)", "ProgramW6432"):
        add(read_env_path(name))
    for letter in "CDEFGHIJKLMNOPQRSTUVWXYZ":
        drive = Path(f"{letter}:\\")
        try:
            if drive.is_dir():
                roots.append(drive)
        except OSError:
            continue
    return roots


_ASAR_CACHE: list[Path] = []
_PACKAGE_DIR_CACHE: list[Path] = []


def find_asar_files(refresh: bool = False) -> list[Path]:
    """app.asar archives belonging to a DSH desktop installation."""
    if _ASAR_CACHE and not refresh:
        return list(_ASAR_CACHE)
    found: list[Path] = []

    def add(candidate: Path | None) -> None:
        if candidate is None:
            return
        try:
            if candidate.is_file() and candidate not in found:
                found.append(candidate)
        except OSError:
            return

    add(config_path(load_config(), "appAsar", must_exist=True))
    for candidate in running_app_asars():
        add(candidate)
    if not found:
        budget = [0]
        for root in install_search_roots():
            add(root / "resources" / "app.asar")
            for directory in iter_dirs(root, depth=2, budget=budget):
                add(directory / "resources" / "app.asar")
    _ASAR_CACHE[:] = found
    return list(found)


def dsh_package_dirs(refresh: bool = False) -> list[Path]:
    """On-disk dsh installations: dirs that hold a `node_modules` or a checkout."""
    if _PACKAGE_DIR_CACHE and not refresh:
        return list(_PACKAGE_DIR_CACHE)
    found: list[Path] = []

    def add(candidate: Path) -> None:
        try:
            if candidate.is_dir() and candidate not in found:
                found.append(candidate)
        except OSError:
            return

    for home in dsh_homes():
        add(home)
        profiles = home / "profiles"
        if profiles.is_dir():
            try:
                for profile in profiles.iterdir():
                    add(profile / "node_modules")
            except OSError:
                pass
    budget = [0]
    for root in install_search_roots():
        add(root)
        add(root / "node_modules")
        for directory in iter_dirs(root, depth=2, budget=budget):
            add(directory)
            add(directory / "node_modules")
    _PACKAGE_DIR_CACHE[:] = found
    return list(found)


def read_asar_member(asar: Path, member: str) -> bytes | None:
    """Read one file out of an Electron asar archive, without dependencies."""
    try:
        with Path(asar).open("rb") as handle:
            head = handle.read(16)
            if len(head) < 16:
                return None
            _, _, _, json_len = struct.unpack("<4I", head)
            if not 0 < json_len < 64 * 1024 * 1024:
                return None
            index = json.loads(handle.read(json_len).decode("utf-8", "replace").rstrip("\x00"))
            base = 16 + json_len
            node: object = index
            for part in member.strip("/").split("/"):
                node = (node.get("files") or {}).get(part) if isinstance(node, dict) else None
                if node is None:
                    return None
            if not isinstance(node, dict) or "files" in node:
                return None
            if node.get("unpacked"):
                sibling = Path(f"{asar}.unpacked") / member.strip("/")
                return sibling.read_bytes() if sibling.is_file() else None
            handle.seek(base + int(node.get("offset", 0)))
            return handle.read(int(node.get("size", 0)))
    except (OSError, ValueError, KeyError, TypeError, struct.error):
        return None


def list_asar_members(asar: Path, prefix: str) -> list[str]:
    """Every file path under `prefix` in an asar archive."""
    try:
        with Path(asar).open("rb") as handle:
            head = handle.read(16)
            if len(head) < 16:
                return []
            _, _, _, json_len = struct.unpack("<4I", head)
            if not 0 < json_len < 64 * 1024 * 1024:
                return []
            index = json.loads(handle.read(json_len).decode("utf-8", "replace").rstrip("\x00"))
    except (OSError, ValueError, struct.error):
        return []

    members: list[str] = []

    def walk(node: object, path: str) -> None:
        if not isinstance(node, dict):
            return
        for name, entry in (node.get("files") or {}).items():
            if not isinstance(entry, dict):
                continue
            full = f"{path}/{name}"
            if "files" in entry:
                walk(entry, full)
            else:
                members.append(full)

    walk(index, "")
    wanted = "/" + prefix.strip("/") + "/"
    return sorted(member.lstrip("/") for member in members if member.startswith(wanted))


def templates_from_lines(lines: list[str], source: str, where: str) -> list[dict]:
    """One template entry per preset declared in a patch file's lines."""
    templates: list[dict] = []
    for preset in list_presets(lines):
        rows = rows_within(lines, preset["plugins_key"] + 1, preset["end"], preset["plugins_indent"] + 2)
        templates.append(
            {
                "id": preset_label(preset),
                "name": preset.get("name"),
                "source": source,
                "where": where,
                "persona": bool(preset_persona_rows(lines, preset)),
                "plugin_rows": len(rows),
                "lines": lines,
                "preset": preset,
            }
        )
    return templates


def shipped_preset_templates() -> list[dict]:
    """The presets DSH itself ships, read from disk or from app.asar."""
    templates: list[dict] = []
    for directory in dsh_package_dirs():
        for relative in SHIPPED_PRESET_DIRS:
            presets_dir = directory / relative
            if not presets_dir.is_dir():
                continue
            for path in sorted(presets_dir.glob("*.patch.yml")):
                try:
                    lines = split_text(read_text_exact(path))["lines"]
                except OSError:
                    continue
                templates += templates_from_lines(lines, "DSH 自带", str(path))
    for asar in find_asar_files():
        for member in list_asar_members(asar, ASAR_PRESET_PREFIX):
            data = read_asar_member(asar, member)
            if not data:
                continue
            lines = split_text(data.decode("utf-8", "replace"))["lines"]
            templates += templates_from_lines(lines, f"DSH 自带（{asar.name}）", f"{asar}!{member}")
    return templates


def preset_templates() -> list[dict]:
    """Everything this machine can copy a plugin list from, best first."""
    templates: list[dict] = []
    for entry in discover_patch_files():
        if entry.get("error"):
            continue
        try:
            lines = split_text(read_text_exact(entry["path"]))["lines"]
        except OSError:
            continue
        templates += templates_from_lines(lines, f"{entry['profile']} · {entry['source']}", str(entry["path"]))
    templates += shipped_preset_templates()

    seen: set[tuple[str, str]] = set()
    unique: list[dict] = []
    for template in templates:
        key = (str(template["where"]), str(template["id"]))
        if key in seen:
            continue
        seen.add(key)
        unique.append(template)
    return sorted(unique, key=template_sort_key)


def template_sort_key(template: dict) -> tuple:
    """Best template to copy first: a preset that already works on this machine,
    then DSH's own general-purpose one, then whichever brings the most plugins."""
    shipped = str(template.get("source") or "").startswith("DSH 自带")
    return (
        0 if template.get("persona") else 1,
        0 if not shipped else 1,
        0 if template.get("id") == "standard" else 1,
        -int(template.get("plugin_rows") or 0),
        str(template.get("source") or ""),
        str(template.get("id") or ""),
    )


def pick_template(templates: list[dict] | None = None) -> dict | None:
    candidates = preset_templates() if templates is None else templates
    usable = [template for template in candidates if template.get("plugin_rows")]
    return min(usable, key=template_sort_key) if usable else None


def find_template(key: str) -> dict | None:
    """A template named by preset id, or given as a patch file path."""
    wanted = (key or "").strip()
    if not wanted:
        return None
    path = Path(wanted)
    if path.is_file():
        try:
            lines = split_text(read_text_exact(path))["lines"]
        except OSError:
            return None
        found = templates_from_lines(lines, "指定文件", str(path))
        return min(found, key=template_sort_key) if found else None
    matches = [template for template in preset_templates() if template.get("id") == wanted]
    return min(matches, key=template_sort_key) if matches else None


def describe_templates() -> list[str]:
    return [
        f"{template['id']}（{template.get('name') or '未命名'}，{template.get('plugin_rows')} 个插件行"
        f"{'，有人设' if template.get('persona') else '，无人设'}）　来自 {template['source']}"
        for template in preset_templates()
    ]


# ── 新建 / 删除预设结构 ──────────────────────────────────────────────────────


def next_order() -> int:
    orders = []
    for entry in discover_patch_files():
        for preset in entry.get("presets") or []:
            try:
                orders.append(int(str(preset.get("order"))))
            except (TypeError, ValueError):
                continue
    return max(orders, default=0) + 1


def preset_id_owner(preset_id: str, target: Target) -> str | None:
    """Where an existing preset (or any loader row) already claims this id."""
    row_id = f"preset-{preset_id}"
    for entry in discover_patch_files():
        for preset in entry.get("presets") or []:
            if preset.get("preset_id") == preset_id or preset.get("row_id") == row_id:
                return str(entry["path"])
    try:
        lines = split_text(read_text_exact(target.patch_file))["lines"]
    except OSError:
        return None
    for preset in list_presets(lines):
        if preset.get("preset_id") == preset_id or preset.get("row_id") == row_id:
            return str(target.patch_file)
    # any other row targeting this loader id counts as taken, too
    for index, line in enumerate(lines):
        match = ROW_RE.match(line)
        if match and match.group(2) == row_id:
            return str(target.patch_file)
    return None


def insert_block_into_lines(lines: list[str], block: list[str]) -> tuple[list[str], bool]:
    """Append a new top-level patch entry, or fill in an empty `[]` list."""
    for index, line in enumerate(lines):
        if line.strip() == "[]":
            return lines[:index] + list(block) + lines[index + 1:], True
    body = list(lines)
    while body and is_blank(body[-1]):
        body.pop()
    tail = lines[len(body):]
    return body + ([""] if body else []) + list(block) + tail, False


def select_default_preset(lines: list[str], preset_id: str) -> tuple[list[str], bool]:
    """Point DSH's preset registry at this preset, when the file has that row."""
    for row in top_level_rows(lines):
        name = scalar_text(lines, row, "name")
        if not (row["id"].startswith("agent-preset-registry") or name in REGISTRY_PACKAGES):
            continue
        field = read_scalar(lines, row, "selectedDefault")
        if field.get("found"):
            index, indent = field["key_index"], field["field_indent"]
            return lines[:index] + [" " * indent + f"selectedDefault: {yaml_scalar(preset_id)}"] + lines[index + 1:], True
        config_indent = row["indent"] + 2
        insert_at = -1
        for cursor in range(row["index"] + 1, row["end"]):
            if re.match(r"^\s*config:\s*$", lines[cursor]) and indent_of(lines[cursor]) == config_indent:
                insert_at = cursor + 1
                break
        if insert_at == -1:
            insert_at = row["index"] + 1
            config_indent = row["indent"]
        return lines[:insert_at] + [" " * (config_indent + 2) + f"selectedDefault: {yaml_scalar(preset_id)}"] + lines[insert_at:], True
    return lines, False


def set_row_id(lines: list[str], row_id: str) -> list[str]:
    """Rename the preset's own loader row (`- id: preset-...`)."""
    preset = _sole_preset(lines)
    match = ROW_RE.match(lines[preset["start"]])
    if match is None:
        raise ValueError("模板的 preset 行无法识别")
    indent = len(match.group(1))
    return lines[: preset["start"]] + [" " * indent + f"- id: {row_id}"] + lines[preset["start"] + 1 :]


def preset_declaration(
    template: dict,
    preset_id: str,
    name: str,
    description: str | None,
    order: int,
    text: str,
    personas_dir: Path,
) -> tuple[list[str], dict]:
    """The `- insert:` block for a new preset: template plugins, new persona text."""
    block = template_block_lines(template["lines"], template["preset"])
    block = set_row_id(block, f"preset-{preset_id}")
    block = set_config_scalar(block, "id", yaml_scalar(preset_id))
    block = set_config_scalar(block, "name", yaml_scalar(name))
    if description:
        block = set_config_scalar(block, "description", yaml_scalar(description))
    block = set_config_scalar(block, "order", str(order))
    block = ensure_persona_row(block)
    dummy = Target(Path("<new preset>"), personas_dir, preset_id=preset_id)
    block, report = replace_persona(block, dummy, canonical_text(text))
    return block, report


def differing_line_pairs(left: str, right: str) -> list[tuple[str, str]]:
    """Line-by-line differences between two texts, or a count mismatch note."""
    a, b = left.splitlines(), right.splitlines()
    if len(a) != len(b):
        return [("<行数不同>", f"{len(a)} vs {len(b)}")]
    return [(x, y) for x, y in zip(a, b) if x != y]


def is_ordered_subsequence(needles: list[str], haystack: list[str]) -> bool:
    """True when every needle line still appears in haystack, in the same order."""
    cursor = 0
    for needle in needles:
        cursor = next((i for i in range(cursor, len(haystack)) if haystack[i] == needle), -1)
        if cursor == -1:
            return False
        cursor += 1
    return True


def create_preset(
    target: Target,
    preset_id: str,
    name: str | None = None,
    description: str | None = None,
    template: dict | None = None,
    text: str | None = None,
    order: int | None = None,
    select: bool = False,
) -> dict:
    """Declare a brand-new editable persona preset in the target patch file.

    The plugin list is copied verbatim from a template — an existing preset on
    this machine, or one DSH itself ships — so the new preset only names plugin
    packages that this installation actually has. Only the persona text is new.
    """
    wanted = (preset_id or "").strip()
    if not PRESET_ID_RE.match(wanted):
        raise ValueError("preset id 只能用小写字母、数字和连字符（例如 my-persona）")
    if not target.patch_file.is_file():
        raise FileNotFoundError(f"补丁文件不存在：{target.patch_file}")

    owner = preset_id_owner(wanted, target)
    if owner:
        raise ValueError(f"id「{wanted}」已经被这个 preset 用了：{owner}")

    chosen = template or pick_template()
    if chosen is None:
        raise LookupError(
            "没找到可以复制的模板：这台机器上还没有任何 preset，也读不到 DSH 自带的 presets。"
            "可以先用 --template 指定一个含 preset 的补丁文件。"
        )

    initial = canonical_text(text if text and text.strip() else STARTER_PERSONA)
    used_order = order if order is not None else next_order()
    block, persona_report = preset_declaration(
        chosen,
        wanted,
        name or wanted,
        description,
        used_order,
        initial,
        target.personas_dir,
    )

    original = read_text_exact(target.patch_file)
    parts = split_text(original)
    new_lines, filled_marker = insert_block_into_lines(parts["lines"], block)
    appended = join_text({**parts, "lines": new_lines})
    rendered = appended
    selected = False
    if select:
        new_lines, selected = select_default_preset(new_lines, wanted)
        rendered = join_text({**parts, "lines": new_lines})

    # Reading the result back must return exactly what was written, and no
    # preset that was there before may disappear.
    verified_lines = split_text(rendered)["lines"]
    info = inspect_persona(verified_lines, Target(Path("<verify>"), target.personas_dir, preset_id=wanted))
    if info["text"] != initial or info["preset_label"] != wanted:
        raise ValueError("写回后重新读取的新 preset 与输入不一致，已放弃写入")
    row_matches = [preset for preset in list_presets(verified_lines) if preset["row_id"] == f"preset-{wanted}"]
    if len(row_matches) != 1 or row_matches[0]["preset_id"] != wanted:
        raise ValueError("新 preset 的加载行 id 不对，已放弃写入")
    before = {preset_label(preset) for preset in list_presets(parts["lines"])}
    after = {preset_label(preset) for preset in list_presets(split_text(rendered)["lines"])}
    if not before <= after:
        raise ValueError("原有 preset 发生了变化，已放弃写入")
    if filled_marker:
        # A bare `[]` list became the first real entry: every other original
        # line must still be there, in order.
        kept = [line for line in parts["lines"] if line.strip() != "[]"]
        if not is_ordered_subsequence(kept, split_text(appended)["lines"]):
            raise ValueError("替换空列表时弄丢了原有内容，已放弃写入")
    elif not appended.startswith(original):
        raise ValueError("新块不是追加在文件末尾，已放弃写入")
    if selected:
        # Pointing the registry at the new preset may rewrite exactly that key.
        for old_line, new_line in differing_line_pairs(appended, rendered):
            if "selectedDefault" not in old_line or "selectedDefault" not in new_line:
                raise ValueError("除了选中项以外还改动了别的内容，已放弃写入")

    backup = backup_patch(target.patch_file, target.backups_dir)
    try:
        atomic_write(target.patch_file, rendered)
    except OSError as error:
        try:
            target.patch_file.write_bytes(backup.read_bytes())
        except OSError:
            pass
        raise RuntimeError(f"写入失败：{error}") from error

    return {
        "ok": True,
        "preset_label": wanted,
        "path": target.patch_file,
        "backup": backup,
        "template": f"{chosen['id']}（{chosen['source']}）",
        "template_where": chosen["where"],
        "field": persona_report.get("field"),
        "filled_empty_list": filled_marker,
        "selected_default": selected,
        "order": used_order,
        "length": len(initial),
    }


def parent_row(lines: list[str], index: int) -> dict | None:
    """The closest enclosing row of a row — `- insert:` for a nested preset."""
    indent = indent_of(lines[index])
    for cursor in range(index - 1, -1, -1):
        if is_blank(lines[cursor]) or indent_of(lines[cursor]) >= indent:
            continue
        match = ROW_RE.match(lines[cursor]) or INSERT_KEY_RE.match(lines[cursor])
        if match is None:
            continue
        parent_indent = indent_of(lines[cursor])
        if parent_indent < indent:
            return {
                "index": cursor,
                "indent": parent_indent,
                "end": block_end(lines, cursor, parent_indent),
                "key": match.group(1) if INSERT_KEY_RE.match(lines[cursor]) else None,
            }
    return None


def child_row_count(lines: list[str], parent: dict, child_indent: int) -> int:
    """How many sibling rows a parent block holds at this child indent.

    The indent is taken from the row itself: this dialect nests list items
    under a key with four spaces (`- insert:` then `    - id: ...`), not two.
    """
    return len(rows_within(lines, parent["index"] + 1, parent["end"], child_indent))


def point_registry_away(lines: list[str], removed_id: str) -> tuple[list[str], bool]:
    """Stop DSH's preset registry from pointing at a preset that is now gone."""
    for row in top_level_rows(lines):
        name = scalar_text(lines, row, "name")
        if not (row["id"].startswith("agent-preset-registry") or name in REGISTRY_PACKAGES):
            continue
        if scalar_text(lines, row, "selectedDefault") != removed_id:
            return lines, False
        field = read_scalar(lines, row, "selectedDefault")
        if not field.get("found"):
            return lines, False
        remaining = [preset_label(preset) for preset in list_presets(lines) if preset_label(preset) != removed_id]
        # The registry's own `default:` is the natural place to land; otherwise
        # the first preset still in the file; with none left, drop the key so
        # DSH falls back to its own default behaviour.
        preferred = scalar_text(lines, row, "default")
        chosen = preferred if preferred in remaining else (remaining[0] if remaining else None)
        if chosen is None:
            return lines[: field["key_index"]] + lines[field["key_index"] + 1:], True
        index, indent = field["key_index"], field["field_indent"]
        return lines[:index] + [" " * indent + f"selectedDefault: {yaml_scalar(chosen)}"] + lines[index + 1:], True
    return lines, False


def has_list_entries(lines: list[str]) -> bool:
    """True when the patch file still declares some top-level list entry."""
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if indent_of(line) == 0:
            return True
    return False


def dsh_process_names() -> list[str]:
    """Names of running processes that look like a DSH instance.

    Windows-only; anywhere else this reports nothing and the guard stays quiet.
    """
    if os.name != "nt":
        return []
    import ctypes
    from ctypes import wintypes

    TH32CS_SNAPPROCESS = 0x00000002
    INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", ctypes.c_long),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", wintypes.WCHAR * 260),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    snapshot = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snapshot == INVALID_HANDLE_VALUE or snapshot is None:
        return []
    found: list[str] = []
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        more = kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
        while more:
            name = entry.szExeFile or ""
            lowered = name.lower()
            if lowered.startswith("deepseek harness") or lowered.startswith("dsh"):
                found.append(name)
            more = kernel32.Process32NextW(snapshot, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(snapshot)
    return found


def recent_session_activity(home: Path | None, seconds: int = 60) -> list[str]:
    """Session logs written within the last `seconds` — DSH is doing something."""
    if home is None:
        return []
    cutoff = time.time() - seconds
    active: list[str] = []
    for log in session_log_files(home):
        try:
            if log.stat().st_mtime >= cutoff:
                active.append(log.parent.name)
        except OSError:
            continue
    return active


def dsh_is_running(home: Path | None) -> tuple[bool, str]:
    """(running, why) — is a DSH instance busy enough that an id change is unsafe?

    Detected by the desktop process (and `dsh ...` helpers), plus any session
    log written in the last minute, which also covers a `dsh web` started from
    a terminal where no recognizable process name shows up.
    """
    processes = dsh_process_names()
    if processes:
        return True, f"检测到 {len(processes)} 个 DSH 进程（{processes[0]}）"
    active = recent_session_activity(home)
    if active:
        return True, f"刚有 {len(active)} 个对话在写入（{active[0]}）"
    return False, ""


class DshRunningError(RuntimeError):
    """Raised when an operation is refused because DSH is running."""


def require_dsh_stopped(home: Path | None, what: str, force: bool) -> None:
    """Refuse the dangerous operations while DSH is running."""
    if force:
        return
    running, why = dsh_is_running(home)
    if not running:
        return
    raise DshRunningError(
        f"{why}，所以现在不改{what}：改了以后已经存在的对话会指向不存在的 preset，"
        "在 DSH 里就打不开了。\n\n"
        "请先退出 DSH（托盘也退），再重试；\n"
        "确实要现在改就用命令行加 --force，改完关掉 DSH 后跑一次 "
        "--retarget-sessions 旧ID:新ID 收尾。"
    )


def remove_preset(target: Target, preset_id: str, force: bool = False) -> dict:
    """Take a preset declaration out of the target file, keeping a backup."""
    if not target.patch_file.is_file():
        raise FileNotFoundError(f"补丁文件不存在：{target.patch_file}")
    require_dsh_stopped(dsh_home_for(target.patch_file), f"（删掉 preset「{preset_id}」后，用到它的对话会打不开）", force)
    original = read_text_exact(target.patch_file)
    parts = split_text(original)
    lines = parts["lines"]
    row_id = f"preset-{preset_id}"
    preset = next(
        (
            candidate
            for candidate in list_presets(lines)
            if candidate["preset_id"] == preset_id or candidate["row_id"] in (preset_id, row_id)
        ),
        None,
    )
    if preset is None:
        raise LookupError(f"{target.patch_file} 里没有 preset「{preset_id}」")

    label = preset_label(preset)
    child_indent = indent_of(lines[preset["start"]])
    start, end = preset["start"], preset["end"]
    parent = parent_row(lines, start)
    dropped_parent = False
    if parent is not None and child_row_count(lines, parent, child_indent) == 1:
        start = parent["index"]
        dropped_parent = True
    if start > 0 and is_blank(lines[start - 1]):
        start -= 1

    others = [candidate for candidate in list_presets(lines) if start <= candidate["start"] < end]
    if len(others) > 1:
        raise ValueError("这一段里不止一个 preset，已放弃删除")

    removed_row_markers = sum(1 for line in lines[start:end] if ROW_RE.match(line))
    trimmed = lines[:start] + lines[end:]
    restored_marker = False
    if not has_list_entries(trimmed):
        # The removed block had filled in an empty `[]` list; put it back so
        # the file stays a valid (empty) patch list.
        trimmed = trimmed[:start] + ["[]"] + trimmed[start:]
        restored_marker = True
    if parts["lines"] and not parts["lines"][-1].strip() and trimmed and trimmed[-1].strip():
        # Removing the last entry must not eat the file's final newline.
        trimmed = trimmed + [""]
    new_lines, registry_moved = point_registry_away(trimmed, label)
    rendered = join_text({**parts, "lines": new_lines})

    if any(preset_label(candidate) == label for candidate in list_presets(split_text(rendered)["lines"])):
        raise ValueError("删除后 preset 仍然在文件里，已放弃写入")
    before = [preset_label(candidate) for candidate in list_presets(lines)]
    after = [preset_label(candidate) for candidate in list_presets(split_text(rendered)["lines"])]
    missing = [key for key in before if key != label and key not in after]
    if missing:
        raise ValueError(f"删除会连带影响 {'、'.join(missing)}，已放弃写入")
    if structure_markers(new_lines)[0] != structure_markers(lines)[0] - removed_row_markers:
        raise ValueError("删除的行数不对，已放弃写入")
    if registry_moved:
        for old_line, new_line in differing_line_pairs(join_text({**parts, "lines": trimmed}), rendered):
            if "selectedDefault" not in old_line or "selectedDefault" not in new_line:
                raise ValueError("除了选中项以外还改动了别的内容，已放弃写入")

    backup = backup_patch(target.patch_file, target.backups_dir)
    try:
        atomic_write(target.patch_file, rendered)
    except OSError as error:
        try:
            target.patch_file.write_bytes(backup.read_bytes())
        except OSError:
            pass
        raise RuntimeError(f"写入失败：{error}") from error

    return {
        "ok": True,
        "removed": label,
        "path": target.patch_file,
        "backup": backup,
        "dropped_insert_block": dropped_parent,
        "restored_empty_list": restored_marker,
        "registry_moved": registry_moved,
        "remaining": after,
    }


def registry_selected(lines: list[str]) -> str | None:
    """The config id DSH's preset registry currently points at, if present here."""
    for row in top_level_rows(lines):
        name = scalar_text(lines, row, "name")
        if row["id"].startswith("agent-preset-registry") or name in REGISTRY_PACKAGES:
            return scalar_text(lines, row, "selectedDefault") or None
    return None


def _set_preset_meta_line(lines: list[str], preset: dict, key: str, value: str | None) -> tuple[list[str], bool]:
    """Set, insert, or (with value None meaning "keep") a config scalar.

    An empty-string value removes the line; a missing line is inserted just
    before `plugins:` so it stays directly under `config:`.
    """
    indent = preset["plugins_indent"]
    pattern = re.compile(rf"^(\s*){re.escape(key)}:\s*(.*)$")
    for cursor in range(preset["start"] + 1, preset["end"]):
        match = pattern.match(lines[cursor])
        if match and len(match.group(1)) == indent:
            if value is None:
                return lines, False
            return lines[:cursor] + [" " * indent + f"{key}: {value}"] + lines[cursor + 1:], True
    if value is None:
        return lines, False
    return lines[: preset["plugins_key"]] + [" " * indent + f"{key}: {value}"] + lines[preset["plugins_key"] :], True


def _remove_preset_meta_line(lines: list[str], preset: dict, key: str) -> tuple[list[str], bool]:
    indent = preset["plugins_indent"]
    pattern = re.compile(rf"^(\s*){re.escape(key)}:\s*(.*)$")
    for cursor in range(preset["start"] + 1, preset["end"]):
        match = pattern.match(lines[cursor])
        if match and len(match.group(1)) == indent:
            return lines[:cursor] + lines[cursor + 1:], True
    return lines, False


EDITABLE_META_RE = re.compile(r"selectedDefault|^\s*-\s+id:|^\s*(?:id|name|description|order):")


# ── 会话记录里的 preset id（改名后老对话要能继续打开） ───────────────────────
#
# DSH 会把每个会话选用的 preset id 写进会话日志的头部记录
# （`{"type":"session",...,"agentPreset":"test"}`），resume 时按这个 id 查表，
# 找不到就报 `Unknown agent preset: test`。所以改 preset id 时必须把已有会话
# 记录里的这个字段一起改掉。
#
# 会话日志是「一帧一条记录」的 zstd 拼接文件（每次追加写一个新 frame）。
# 只重写含 `agentPreset` 的那几帧，其余帧逐字节保留；改完再读回来逐帧核对。

ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"
SESSION_HEADER_KEYS = ("agentPreset",)
ZSTD_IMPORT_ERROR = ""  # filled in by zstd_codec(), shown by --check


def zstd_codec() -> tuple[object, object] | None:
    """(compress, decompress) for zstd, or None when unavailable.

    Python 3.14+ has zstd in the standard library. On older versions the
    package to prefer is `zstandard`: its C backend is a separate module name,
    so a frozen exe picks it up cleanly (pyzstd wraps `backports.zstd`, whose
    pure-Python fallback shadows the C extension once frozen). Whatever fails is
    remembered so `--check` can say something better than "not available".
    """
    global ZSTD_IMPORT_ERROR
    problems: list[str] = []
    try:  # Python 3.14+ ships zstd in the standard library
        from compression import zstd  # type: ignore[import-not-found]

        return zstd.compress, zstd.decompress
    except Exception as error:  # noqa: BLE001 - any import/runtime problem falls through
        problems.append(_import_problem("compression.zstd", error))
    try:
        import zstandard

        compressor = zstandard.ZstdCompressor()
        decompressor = zstandard.ZstdDecompressor()

        def compress(data: bytes) -> bytes:
            return compressor.compress(data)

        def decompress(data: bytes) -> bytes:
            # A frame appended to a stream need not carry its content size, so
            # go through a decompression object instead of the one-shot call.
            return decompressor.decompressobj().decompress(data)

        return compress, decompress
    except Exception as error:  # noqa: BLE001
        problems.append(_import_problem("zstandard", error))
    try:
        import pyzstd

        return pyzstd.compress, pyzstd.decompress
    except Exception as error:  # noqa: BLE001
        problems.append(_import_problem("pyzstd", error))
    ZSTD_IMPORT_ERROR = "；".join(problems)
    return None


def _import_problem(name: str, error: BaseException) -> str:
    detail = f"{type(error).__name__}: {error}"
    cause = error.__cause__ or error.__context__
    if cause is not None and str(cause) not in detail:
        detail += f"（起因：{type(cause).__name__}: {cause}）"
    return f"{name} → {detail}"


def split_zstd_frames(data: bytes) -> list[bytes]:
    """Split a concatenated zstd stream into frames without decompressing."""
    frames: list[bytes] = []
    position = 0
    total = len(data)
    while position < total:
        start = position
        if data[position : position + 4] != ZSTD_MAGIC:
            raise ValueError(f"第 {position} 字节不是 zstd 帧头")
        position += 4
        if position >= total:
            raise ValueError("帧头不完整")
        descriptor = data[position]
        position += 1
        fcs_flag = descriptor >> 6
        single_segment = (descriptor >> 5) & 1
        has_checksum = (descriptor >> 2) & 1
        dict_id_flag = descriptor & 3
        if not single_segment:
            position += 1  # window descriptor
        position += (0, 1, 2, 4)[dict_id_flag]
        if fcs_flag == 0 and single_segment:
            position += 1
        else:
            position += (0, 2, 4, 8)[fcs_flag]
        while True:
            if position + 3 > total:
                raise ValueError("块头不完整")
            header = data[position] | (data[position + 1] << 8) | (data[position + 2] << 16)
            position += 3
            last_block = header & 1
            block_type = (header >> 1) & 3
            block_size = header >> 3
            position += 1 if block_type == 1 else block_size  # RLE blocks store one byte
            if last_block:
                break
        if has_checksum:
            position += 4
        if position > total:
            raise ValueError("帧结尾超出文件末尾")
        frames.append(data[start:position])
    return frames


def dsh_home_for(patch_file: Path) -> Path | None:
    """The DSH home directory a patch file belongs to (patch profiles/<name>/...).

    Deliberately requires a `profiles` component in the path: a patch copied to
    a scratch directory must never make this tool touch the real session store
    (tests rely on that).
    """
    for candidate in [patch_file, *patch_file.parents]:
        if candidate.name == "profiles":
            return candidate.parent
    return None


def session_log_files(home: Path) -> list[Path]:
    """Every session log under <home>/sessions (workspace dirs are one level deep)."""
    root = Path(home) / "sessions"
    if not root.is_dir():
        return []
    found: list[Path] = []
    for workspace in sorted(root.iterdir()):
        if not workspace.is_dir() or workspace.name.startswith("."):
            continue
        for session in sorted(workspace.iterdir()):
            if not session.is_dir():
                continue
            for log in sorted(session.glob("session*.jsonl.zstd")):
                found.append(log)
    return found


def retarget_sessions(
    home: Path,
    old_id: str,
    new_id: str,
    skip_recent_seconds: int = 120,
) -> dict:
    """Rewrite the preset id stored in existing session records.

    Only frames whose JSON carries an `agentPreset` field naming `old_id` are
    touched; every other frame is kept byte for byte. Sessions DSH wrote within
    `skip_recent_seconds` are left alone (they are in use and would race), and
    projection caches naming the old id are backed up and dropped so DSH folds
    them again from the fixed log.
    """
    codec = zstd_codec()
    result: dict = {
        "available": codec is not None,
        "logs": 0,
        "frames": 0,
        "caches": 0,
        "skipped": [],
        "backup": None,
        "error": None,
    }
    if codec is None:
        result["error"] = "本机没有可用的 zstd（Python 3.14+ 或 pyzstd），会话记录没有改"
        return result
    compress, decompress = codec
    pattern = re.compile(rb'("agentPreset"\s*:\s*)"' + re.escape(old_id.encode("utf-8")) + rb'"')
    now = time.time()
    logs = session_log_files(home)
    changed: list[tuple[Path, bytes, int, int]] = []
    frames_touched = 0
    skipped: list[str] = []
    for log in logs:
        try:
            raw = log.read_bytes()
        except OSError:
            continue
        if now - log.stat().st_mtime < skip_recent_seconds and pattern.search(
            _safe_decompress(decompress, raw)
        ):
            skipped.append(log.parent.name)
            continue
        try:
            frames = split_zstd_frames(raw)
        except ValueError:
            continue
        rebuilt: list[bytes] = []
        touched = 0
        text_total = 0
        for frame in frames:
            try:
                text = decompress(frame)
            except Exception:  # noqa: BLE001 - not our frame
                rebuilt.append(frame)
                continue
            text_total += len(text)
            if not pattern.search(text):
                rebuilt.append(frame)
                continue
            touched += 1
            rebuilt.append(compress(pattern.sub(rb'\1"' + new_id.encode("utf-8") + b'"', text)))
        if not touched:
            continue
        frames_touched += touched
        # expected size of the fixed log's text: only the id got longer/shorter
        expected = text_total + touched * (len(new_id.encode("utf-8")) - len(old_id.encode("utf-8")))
        changed.append((log, b"".join(rebuilt), len(frames), expected))

    if changed or skipped:
        result["skipped"] = skipped

    # Projection caches that still name the old id must go even when no log
    # needed rewriting: DSH folds them again from the (already fixed) log.
    cache_dir = Path(home) / "storages" / "session_projcache" / "sessions"
    cache_stamp = time.strftime("%Y-%m-%dT%H-%M-%S")
    dropped = 0
    if cache_dir.is_dir():
        live = set(skipped)
        for cache in sorted(cache_dir.glob("*.json")):
            if cache.stem in live:
                continue
            try:
                text = cache.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if f'"val": "{old_id}"' not in text and f'"val":"{old_id}"' not in text:
                continue
            try:
                cache_backup = Path(home) / "preset-id-backups" / cache_stamp
                cache_backup.mkdir(parents=True, exist_ok=True)
                shutil.copy2(cache, cache_backup / f"{cache.stem}.projcache.json")
                cache.unlink()
                dropped += 1
            except OSError:
                continue
    result["caches"] = dropped

    if not changed:
        return result

    stamp = time.strftime("%Y-%m-%dT%H-%M-%S")
    backup_root = Path(home) / "preset-id-backups" / stamp
    for log, output, _, _ in changed:
        try:
            backup_root.mkdir(parents=True, exist_ok=True)
            shutil.copy2(log, backup_root / f"{log.parent.name}.jsonl.zstd")
            atomic_write_bytes(log, output)
        except OSError as error:
            result["error"] = f"写入失败：{error}"
            return result
    result["backup"] = backup_root
    result["logs"] = len(changed)

    # Verify by re-reading: same frame count, no old id left, and the decoded
    # text is exactly as long as the id substitution implies (a truncated frame
    # would still decode without error, so the length is the real check).
    ok = True
    for log, _, original_frames, expected_text in changed:
        try:
            after = split_zstd_frames(log.read_bytes())
        except (OSError, ValueError):
            ok = False
            break
        if len(after) != original_frames:
            ok = False
            break
        after_text = 0
        for frame in after:
            try:
                text = decompress(frame)
            except Exception:  # noqa: BLE001
                continue
            after_text += len(text)
            if pattern.search(text):
                ok = False
                break
        if not ok or after_text != expected_text:
            ok = False
            break
    result["frames"] = frames_touched
    if not ok:
        result["error"] = "改完后核对不一致，请用备份还原"
    return result


def _safe_decompress(decompress: object, data: bytes) -> bytes:
    try:
        return decompress(data)  # type: ignore[operator]
    except Exception:  # noqa: BLE001
        return b""


def edit_preset(
    target: Target,
    preset_id: str | None = None,
    name: str | None = None,
    description: str | None = None,
    order: int | str | None = None,
    skip_sessions: bool = False,
    force: bool = False,
) -> dict:
    """Edit a preset's metadata: id, display name, description, order.

    Passing None keeps a field as it is. An empty string removes the
    name/description/order line. Changing the id rewrites both the loader row
    (`- id: preset-...`) and `config.id`, moves the registry's selectedDefault
    when it pointed here, refuses ids some other preset already uses, and
    rewrites the existing session records that named the old id.

    Renaming (and deleting) is refused while DSH is running unless `force` is
    set: those are the two operations that can orphan an existing conversation.
    """
    if not target.patch_file.is_file():
        raise FileNotFoundError(f"补丁文件不存在：{target.patch_file}")
    home = dsh_home_for(target.patch_file)
    wanted = (preset_id or "").strip()
    original = read_text_exact(target.patch_file)
    parts = split_text(original)
    lines = parts["lines"]
    preset = resolve_preset(lines, target)
    if preset is None:
        raise LookupError(f"{target.patch_file} 里没有可以编辑的 preset")

    old_label = preset_label(preset)
    old = {
        "id": old_label,
        "name": preset.get("name") or "",
        "description": preset.get("description") or "",
        "order": str(preset.get("order") or ""),
    }

    wanted_id = (preset_id or "").strip()
    if wanted_id and wanted_id != old_label:
        require_dsh_stopped(home, f" preset id（{old_label} → {wanted_id}）", force)
        if not PRESET_ID_RE.match(wanted_id):
            raise ValueError("preset id 只能用小写字母、数字和连字符（例如 my-persona）")
        owner = preset_id_owner(wanted_id, target)
        if owner:
            raise ValueError(f"id「{wanted_id}」已经被这个 preset 用了：{owner}")
    else:
        wanted_id = ""

    changes: list[tuple[str, str | None]] = []  # (key, rendered value; None = remove line)
    if wanted_id:
        changes.append(("id", wanted_id))
    for key, requested, current in (
        ("name", name, old["name"]),
        ("description", description, old["description"]),
        ("order", order, old["order"]),
    ):
        if requested is None:
            continue
        text = str(requested).strip()
        if text == current:
            continue  # untouched: rewriting it would only churn the file
        if text == "":
            changes.append((key, None))  # remove the line
        elif key == "order":
            if not re.fullmatch(r"-?\d+", text):
                raise ValueError("order 必须是整数（例如 3）")
            changes.append((key, str(int(text))))
        else:
            changes.append((key, yaml_scalar(text)))
    if not changes:
        raise ValueError("没有需要修改的内容：填一个和现在不一样的值")

    mutated = list(lines)
    current_id = old_label
    if wanted_id:
        # 1:1 line replacement first (no index shift), then the config id.
        mutated[preset["start"]] = " " * indent_of(lines[preset["start"]]) + f"- id: preset-{wanted_id}"
        mutated, _ = _set_preset_meta_line(mutated, preset, "id", wanted_id)
        current_id = wanted_id

    def find_row(source: list[str]) -> dict | None:
        return next(
            (
                candidate
                for candidate in list_presets(source)
                if candidate["preset_id"] == current_id or candidate["row_id"] == f"preset-{current_id}"
            ),
            None,
        )

    # Insertions/removals shift line indices, so each step re-locates the row.
    for key, value in changes:
        if key == "id":
            continue
        current = find_row(mutated)
        if current is None:
            raise ValueError(f"改着改着找不到 preset 行了（{key}），已放弃写入")
        if value is None:
            mutated, _ = _remove_preset_meta_line(mutated, current, key)
        else:
            mutated, _ = _set_preset_meta_line(mutated, current, key, value)
    selected_moved = False
    if wanted_id and registry_selected(mutated) == old_label:
        mutated, selected_moved = select_default_preset(mutated, wanted_id)

    rendered = join_text({**parts, "lines": mutated})
    verified = split_text(rendered)["lines"]

    # Only id/name/description/order/selectedDefault lines may appear or
    # disappear (inserting a line shifts everything after it, so a per-index
    # diff is meaningless here); the preset lineup and their persona rows must
    # be the same, with the renamed one swapped, and rows must survive intact.
    removed = Counter(split_text(original)["lines"]) - Counter(mutated)
    added = Counter(mutated) - Counter(split_text(original)["lines"])
    for line in list(removed) + list(added):
        if not EDITABLE_META_RE.search(line):
            raise ValueError("改动了 id、名字、说明之外的内容，已放弃写入")
    before_presets = list_presets(lines)
    after_presets = list_presets(verified)
    if [candidate.get("persona_rows") for candidate in after_presets] != [
        candidate.get("persona_rows") for candidate in before_presets
    ]:
        raise ValueError("persona 行发生了变化，已放弃写入")
    before_labels = [preset_label(candidate) for candidate in before_presets]
    after_labels = [preset_label(candidate) for candidate in after_presets]
    expected = [wanted_id if label == old_label and wanted_id else label for label in before_labels]
    if after_labels != expected:
        raise ValueError("改名影响了其他 preset，已放弃写入")
    if structure_markers(mutated) != structure_markers(lines):
        raise ValueError("preset 行结构发生了变化，已放弃写入")
    final_id = wanted_id or old_label
    row_matches = [candidate for candidate in after_presets if candidate["row_id"] == f"preset-{final_id}"]
    if len(row_matches) != 1 or row_matches[0]["preset_id"] != final_id:
        raise ValueError("改名后找不到新 id 的 preset 行，已放弃写入")

    backup = backup_patch(target.patch_file, target.backups_dir)
    try:
        atomic_write(target.patch_file, rendered)
    except OSError as error:
        try:
            target.patch_file.write_bytes(backup.read_bytes())
        except OSError:
            pass
        raise RuntimeError(f"写入失败：{error}") from error

    sessions = None
    if wanted_id and not skip_sessions:
        if home is not None:
            sessions = retarget_sessions(home, old_label, wanted_id)

    return {
        "ok": True,
        "old": old_label,
        "new": final_id,
        "name": next((value for key, value in changes if key == "name"), old["name"] or None),
        "description": next((value for key, value in changes if key == "description"), old["description"] or None),
        "order": next((value for key, value in changes if key == "order"), old["order"] or None),
        "path": target.patch_file,
        "backup": backup,
        "selected_moved": selected_moved,
        "sessions": sessions,
    }


# ── 命令行 ───────────────────────────────────────────────────────────────────


def stale_session_presets(home: Path | None, known_ids: set[str]) -> dict[str, list[str]]:
    """Session logs whose preset id is not defined anywhere: id -> session names.

    These are the conversations that will answer `Unknown agent preset` when
    opened, which is exactly what a rename used to leave behind.
    """
    if home is None:
        return {}
    codec = zstd_codec()
    if codec is None:
        return {}
    _, decompress = codec
    pattern = re.compile(rb'"agentPreset"\s*:\s*"([^"]+)"')
    stale: dict[str, list[str]] = {}
    for log in session_log_files(home):
        try:
            frames = split_zstd_frames(log.read_bytes())
        except (OSError, ValueError):
            continue
        header_id = ""
        try:
            header_id = json.loads(decompress(frames[0]).decode("utf-8")).get("agentPreset") or ""
        except Exception:  # noqa: BLE001
            continue
        if not header_id or header_id in known_ids:
            continue
        # only report it when the *effective* preset (last switch) is missing too
        effective = header_id
        for frame in frames:
            try:
                text = decompress(frame)
            except Exception:  # noqa: BLE001
                continue
            for value in pattern.findall(text):
                effective = value.decode("utf-8", "replace")
        if effective in known_ids:
            continue
        stale.setdefault(effective, []).append(log.parent.name)
    return stale


def known_preset_ids() -> set[str]:
    """Every preset id this machine can actually resolve.

    Session records only store `config.id` of the preset they ran, so both the
    loader row ids (`preset-xxx`) and the config ids are collected, from the
    patch files on disk *and* from the presets DSH ships inside app.asar
    (`standard` and friends live there, not in any patch file).
    """
    known: set[str] = set()
    for entry in discover_patch_files():
        for candidate in entry.get("presets") or []:
            label = preset_label(candidate)
            if label:
                known.add(label)
            row_id = candidate.get("row_id")
            if row_id:
                known.add(row_id)
    for template in shipped_preset_templates():
        label = preset_label(template.get("preset") or template)
        if label:
            known.add(label)
    return known


def _describe_stale_sessions(home: Path | None) -> str:
    """One line (plus details) about conversations whose preset id is gone."""
    if home is None:
        return "不在 profiles 目录下，没检查"
    if zstd_codec() is None:
        return "本机没有 zstd，没检查"
    stale = stale_session_presets(home, known_preset_ids())
    if not stale:
        return "无（每个对话的 preset id 都还在）"
    total = sum(len(names) for names in stale.values())
    lines = [f"{total} 个 —— preset id 已经不存在了，这些对话打不开"]
    for preset_id, names in sorted(stale.items()):
        lines.append(f"      * 「{preset_id}」：{len(names)} 个（例如 {names[0]}）")
    lines.append("      先退出 DSH，再跑：人设编辑-cli.exe --retarget-sessions 旧ID:新ID")
    return "\n".join(lines)


def describe_session_fix(report: dict | None) -> list[str]:
    """Readable lines about what happened to the existing session records."""
    if report is None:
        return []
    lines: list[str] = []
    if not report.get("available"):
        lines.append(f"对话记录未改：{report.get('error') or '本机没有可用的 zstd'}")
        return lines
    if report.get("logs"):
        lines.append(f"已改 {report['logs']} 个对话记录里的 preset id（共 {report['frames']} 条记录，已备份）")
    if report.get("caches"):
        lines.append(f"顺手清掉 {report['caches']} 个投影缓存，DSH 会自己重建。")
    if report.get("skipped"):
        lines.append(f"跳过 {len(report['skipped'])} 个正在使用的对话（DSH 正开着，等它关掉再跑一次即可）")
    if report.get("error"):
        lines.append(f"注意：{report['error']}")
    if report.get("backup"):
        lines.append(f"对话记录备份：{report['backup']}")
    if not lines:
        lines.append("没有对话记录指向旧 id，不需要改动。")
    return lines


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="人设编辑", description=f"{APP_NAME} {APP_VERSION}")
    parser.add_argument("--patch", help="cordis.patch.yml 的路径（默认自动查找）")
    parser.add_argument("--personas", help="人设文件目录（默认为 exe 同目录的 personas）")
    parser.add_argument("--preset", help="要编辑的 preset id（默认按 DSH 当前选中项自动选择）")
    parser.add_argument("--persona-row", dest="persona_row", help="persona 插件行的 id（默认自动识别）")
    parser.add_argument("--list", dest="list_files", action="store_true", help="列出找到的补丁文件和 preset")
    parser.add_argument("--check", action="store_true", help="打印当前状态，不做修改")
    parser.add_argument("--print", dest="print_text", action="store_true", help="把当前人设打到标准输出")
    parser.add_argument("--set-file", help="用这个文件的正文覆盖当前 preset 的人设")
    parser.add_argument("--revert", action="store_true", help="撤销上一次保存")
    parser.add_argument(
        "--create-preset",
        metavar="ID",
        help="新建一个 preset：复制模板的插件表，人设用 --set-file 的正文（缺省给一段开头）",
    )
    parser.add_argument("--preset-name", help="新建/改名时用的显示名")
    parser.add_argument("--preset-description", help="新建/改名时用的说明")
    parser.add_argument("--preset-order", help="新建/改名时用的排序数字")
    parser.add_argument("--template", help="新建 preset 用的模板：preset id 或补丁文件路径（默认自动挑最合适的）")
    parser.add_argument(
        "--rename-preset",
        metavar="ID",
        nargs="?",
        const="",
        help="编辑当前 preset 的信息：给 ID 就连 id 一起改；配合 --preset-name/-description/-order 修改其余字段",
    )
    parser.add_argument("--select", action="store_true", help="新建后把 DSH 的 selectedDefault 指向它（同文件里有注册行才生效）")
    parser.add_argument("--delete-preset", metavar="ID", help="从补丁文件里删除一个 preset（留备份）")
    parser.add_argument(
        "--no-fix-sessions",
        action="store_true",
        help="改 preset id 时不改写已有对话记录（默认会改，否则那些对话会打不开）",
    )
    parser.add_argument(
        "--retarget-sessions",
        metavar="旧ID:新ID",
        help="只改写对话记录里的 preset id（不动补丁文件），用于修已经改坏的老对话",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="DSH 在运行时也照改（改 preset id / 删 preset / 修对话记录默认会被拒绝）",
    )
    return parser


def describe_presets(entry: dict) -> list[str]:
    """One readable line per preset of a discovered patch file."""
    lines = []
    for preset in entry.get("presets") or []:
        persona = "、".join(preset.get("persona_rows") or []) or "无 persona 行"
        name = preset.get("name") or preset.get("description") or "未命名"
        lines.append(f"{preset_label(preset)}（{name}，{persona}，{preset.get('persona_length', 0)} 字符）")
    return lines


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    target = Target.resolve(args)
    # A windowed build has no stdout; route command-line output through emit().
    action = bool(
        args.list_files
        or args.check
        or args.print_text
        or args.set_file
        or args.revert
        or args.create_preset is not None
        or args.delete_preset
        or args.rename_preset is not None
        or args.retarget_sessions
    )
    out: list[str] = []

    def say(message: str) -> None:
        if action:
            out.append(message)
        else:
            emit(message)

    try:
        if args.list_files:
            entries = discover_patch_files()
            say(f"{APP_NAME} {APP_VERSION}")
            say(f"  DSH 目录：{'、'.join(str(home) for home in dsh_homes()) or '（没找到）'}")
            if not entries:
                say("  没有找到任何 cordis.patch.yml；用 --patch 指定路径。")
                return flush(action, out)
            for entry in entries:
                say(f"  [{entry['profile']}] {entry['path']}（{entry['source']}，{len(entry.get('presets') or [])} 个 preset）")
                for line in describe_presets(entry):
                    say(f"      - {line}")
            say("  可以复制为模板的 preset（--template 用最前面的 id）：")
            templates = describe_templates()
            for line in templates[:8]:
                say(f"      * {line}")
            if len(templates) > 8:
                say(f"      * …还有 {len(templates) - 8} 个")
            return flush(action, out)

        if args.print_text:
            lines = split_text(target.patch_file.read_text(encoding="utf-8"))["lines"]
            text = read_persona(lines, target)
            if action:
                out.append(text)
            else:
                emit(text)
            return flush(action, out)

        if args.check:
            if not target.patch_file.is_file():
                say(f"{APP_NAME} {APP_VERSION}")
                say(f"  补丁文件不存在：{target.patch_file}")
                say("  用 --list 看这台机器上找到了哪些文件，或用 --patch 指定路径。")
                return flush(action, out, 1)
            summary = summarize_patch(target.patch_file)
            lines = split_text(target.patch_file.read_text(encoding="utf-8", errors="replace"))["lines"]
            files = list_persona_files(target.personas_dir)
            say(f"{APP_NAME} {APP_VERSION}")
            say(f"  DSH 目录：{'、'.join(str(home) for home in dsh_homes()) or '（没找到）'}")
            say(f"  补丁文件：{target.patch_file}")
            say(f"  人设目录：{target.personas_dir}")
            say(f"  DSH 选中：{summary.get('selected_default') or '未设置'}")
            try:
                info = inspect_persona(lines, target)
            except LookupError as error:
                say(f"  编辑对象：无 —— {error}")
                say(f"  文件里的 preset：{'、'.join(describe_presets(summary)) or '无'}")
                return flush(action, out)
            say(f"  编辑对象：preset「{info['preset_label']}」（{info['resolved_by']}）")
            say(
                f"  persona ：{info['row_id']}"
                + (f"（{info['package']}）" if info["package"] else "")
                + f"　{info['row_reason']}　字段 {info['field']}"
            )
            say(f"  当前人设：{len(info['text'])} 字符 / {len(info['text'].splitlines())} 行")
            others = [line for line in describe_presets(summary) if not line.startswith(f"{info['preset_label']}（")]
            say(f"  同文件其他 preset：{'、'.join(others) if others else '无'}")
            home = dsh_home_for(target.patch_file)
            say(
                "  对话记录："
                + (f"{home}（改 preset id 时会一并改写）" if home is not None else "不在 profiles 目录下，改 id 时不碰对话记录")
            )
            say(f"  改写对话记录所需 zstd：{'可用' if zstd_codec() else '不可用 —— ' + ZSTD_IMPORT_ERROR}")
            running, why = dsh_is_running(home)
            say(f"  DSH 状态：{'正在运行 —— ' + why if running else '没在运行（改 id / 删 preset 现在可以做）'}")
            say(f"  打不开的对话：{_describe_stale_sessions(home)}")
            say(
                f"  人设文件：{len(files)} 个"
                + ("（" + "、".join(item["name"] for item in files) + "）" if files else "")
            )
            candidates = discover_patch_files()
            if len(candidates) > 1:
                say("  其他候选文件：")
                for entry in candidates[1:]:
                    say(f"    - [{entry['profile']}] {entry['path']}（{len(entry.get('presets') or [])} 个 preset）")
            return flush(action, out)

        if args.create_preset:
            text = None
            if args.set_file:
                source = Path(args.set_file)
                if not source.is_file():
                    say(f"人设文件不存在：{source}")
                    return flush(action, out, 1)
                text = source.read_text(encoding="utf-8")
            chosen_template = None
            if args.template:
                chosen_template = find_template(args.template)
                if chosen_template is None:
                    say(f"没找到模板「{args.template}」：用 --list 看可以复制的 preset id。")
                    return flush(action, out, 1)
            order = None
            if args.preset_order not in (None, ""):
                try:
                    order = int(args.preset_order)
                except ValueError:
                    say(f"order 必须是整数，收到：{args.preset_order}")
                    return flush(action, out, 1)
            result = create_preset(
                target,
                args.create_preset,
                name=args.preset_name,
                description=args.preset_description,
                template=chosen_template,
                text=text,
                order=order,
                select=args.select,
            )
            say(f"已新建 preset「{result['preset_label']}」（模板：{result['template']}）")
            say(f"文件：{result['path']}")
            say(f"初始人设：{result['length']} 字符　order：{result['order']}")
            if result["filled_empty_list"]:
                say("这个文件原本是空列表 []，已用新 preset 填上。")
            if result["selected_default"]:
                say("已把 DSH 的 selectedDefault 指向它。")
            else:
                say("要在 DSH 里用它：开新对话时选这个 preset（或在 DSH 设置里选）。")
            say(f"备份：{result['backup']}")
            say("DSH 会在 1～2 秒内热加载。")
            return flush(action, out)

        if args.delete_preset:
            result = remove_preset(target, args.delete_preset, force=args.force)
            say(f"已删除 preset「{result['removed']}」")
            if result["dropped_insert_block"]:
                say("整个 - insert: 块已一并移除。")
            if result["restored_empty_list"]:
                say("文件恢复为空列表 []。")
            if result["registry_moved"]:
                say("DSH 的 selectedDefault 已改指其他 preset。")
            say(f"备份：{result['backup']}")
            return flush(action, out)

        if args.rename_preset is not None:
            order = None
            if args.preset_order not in (None, ""):
                try:
                    order = int(args.preset_order)
                except ValueError:
                    say(f"order 必须是整数，收到：{args.preset_order}")
                    return flush(action, out, 1)
            result = edit_preset(
                target,
                preset_id=args.rename_preset or None,
                name=args.preset_name,
                description=args.preset_description,
                order=order,
                skip_sessions=args.no_fix_sessions,
                force=args.force,
            )
            if result["old"] != result["new"]:
                say(f"preset id：{result['old']} → {result['new']}")
            for key, title in (("name", "显示名"), ("description", "说明"), ("order", "排序")):
                value = result.get(key)
                if value not in (None, ""):
                    say(f"{title}：{value}")
            if result["selected_moved"]:
                say("DSH 的 selectedDefault 已跟着指向新 id。")
            say(f"备份：{result['backup']}")
            for line in describe_session_fix(result.get("sessions")):
                say(line)
            return flush(action, out)

        if args.retarget_sessions:
            if ":" not in args.retarget_sessions:
                say("格式：--retarget-sessions 旧ID:新ID（例如 test:dafeiyu）")
                return flush(action, out, 1)
            old_id, _, new_id = args.retarget_sessions.partition(":")
            old_id, new_id = old_id.strip(), new_id.strip()
            home = dsh_home_for(target.patch_file)
            if not old_id or not new_id or home is None:
                say("找不到 DSH 家目录（补丁文件不在 profiles 目录下），没有改动。")
                return flush(action, out, 1)
            try:
                require_dsh_stopped(home, " 对话记录", args.force)
            except RuntimeError as error:
                say(str(error))
                return flush(action, out, 1)
            say(f"改写 {home} 下对话记录里的 preset id：{old_id} → {new_id}")
            for line in describe_session_fix(retarget_sessions(home, old_id, new_id)):
                say(line)
            return flush(action, out)

        if args.set_file:
            source = Path(args.set_file)
            if not source.is_file():
                say(f"人设文件不存在：{source}")
                return flush(action, out, 1)
            result = write_persona(target, source.read_text(encoding="utf-8"))
            say(f"已写入 preset「{result['preset_label']}」：{result['previous_length']} → {result['length']} 字符")
            say(f"persona 行：{result['row_id']}　字段：{result['field']}")
            say(f"备份：{result['backup']}")
            say("DSH 会在 1～2 秒内热加载；已存在的会话需要新开一个对话才用上新人设。")
            return flush(action, out)

        if args.revert:
            result = revert_patch(target)
            say(f"已回滚：{result['restored_from']}")
            return flush(action, out)
    except (LookupError, ValueError, FileNotFoundError, RuntimeError, OSError) as error:
        say(f"失败：{error}")
        return flush(action, out, 1)

    return run_gui(target)


def flush(action: bool, out: list[str], code: int = 0) -> int:
    """Deliver buffered command-line output, then return the exit code."""
    if action and out:
        emit("\n".join(out))
    return code


# ── 图形界面 ─────────────────────────────────────────────────────────────────


def run_gui(target: Target, headless: bool = False, probe=None) -> int:
    """Open the window. `headless` builds it, runs one layout pass and closes,
    which is how the self-test exercises widget construction without a flash.
    `probe` receives the editor in headless mode so a test can drive it."""
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk

    class Editor:
        def __init__(self, root: tk.Tk) -> None:
            self.root = root
            self.target = target
            self.loaded_text = ""
            self.file_items: list[dict] = []
            self.patch_entries: list[dict] = []
            self.presets: list[dict] = []
            self.chosen: dict[str, str] = {}

            root.title(f"{APP_NAME} {APP_VERSION}")
            root.geometry("1060x720")
            root.minsize(780, 520)

            outer = ttk.Frame(root, padding=10)
            outer.pack(fill="both", expand=True)

            finder = ttk.Frame(outer)
            finder.pack(fill="x")
            ttk.Label(finder, text="补丁文件：").pack(side="left")
            self.file_box = ttk.Combobox(finder, state="readonly", width=52)
            self.file_box.pack(side="left")
            self.file_box.bind("<<ComboboxSelected>>", lambda _event: self.use_selected_file())
            ttk.Button(finder, text="重新扫描", command=self.rescan).pack(side="left", padx=(6, 2))
            ttk.Button(finder, text="选择 yml…", command=self.pick_file).pack(side="left")

            header = ttk.Frame(outer)
            header.pack(fill="x", pady=(6, 0))
            ttk.Label(header, text="preset：").pack(side="left")
            self.preset_box = ttk.Combobox(header, width=30, state="readonly")
            self.preset_box.pack(side="left")
            self.preset_box.bind("<<ComboboxSelected>>", lambda _event: self.on_preset_selected())
            ttk.Button(header, text="新建预设…", command=self.new_preset_dialog).pack(side="left", padx=(8, 2))
            ttk.Button(header, text="编辑信息…", command=self.edit_preset_dialog).pack(side="left", padx=2)
            ttk.Button(header, text="删除此预设…", command=self.delete_current_preset).pack(side="left")
            ttk.Label(header, text="　写入目标：").pack(side="left")
            self.path_label = ttk.Label(header, text=str(target.patch_file), foreground="#666")
            self.path_label.pack(side="left")

            body = ttk.Frame(outer)
            body.pack(fill="both", expand=True, pady=(8, 6))

            left = ttk.Frame(body)
            left.pack(side="left", fill="both", expand=True)
            self.text = tk.Text(left, wrap="word", undo=True, font=("Microsoft YaHei UI", 11))
            scroll = ttk.Scrollbar(left, command=self.text.yview)
            self.text.configure(yscrollcommand=scroll.set)
            self.text.pack(side="left", fill="both", expand=True)
            scroll.pack(side="right", fill="y")
            self.text.bind("<<Modified>>", self.on_modified)
            self.text.bind("<Control-s>", lambda _event: (self.save(), "break")[1])

            side = ttk.Frame(body, padding=(10, 0, 0, 0))
            side.pack(side="right", fill="y")
            ttk.Label(side, text="已保存的人设").pack(anchor="w")
            self.file_list = tk.Listbox(side, width=34, height=18, exportselection=False)
            self.file_list.pack(fill="both", expand=True, pady=(2, 6))
            self.file_list.bind("<Double-Button-1>", lambda _event: self.load_file())

            ttk.Button(side, text="载入到编辑框", command=self.load_file).pack(fill="x", pady=2)
            ttk.Button(side, text="打开人设文件夹", command=self.open_folder).pack(fill="x", pady=2)

            save_as = ttk.Frame(side)
            save_as.pack(fill="x", pady=(8, 2))
            self.save_name = ttk.Entry(save_as)
            self.save_name.pack(side="left", fill="x", expand=True)
            ttk.Button(save_as, text="另存为", width=8, command=self.save_as).pack(side="left", padx=(4, 0))

            footer = ttk.Frame(outer)
            footer.pack(fill="x")
            self.save_button = ttk.Button(footer, text="保存到预设", command=self.save)
            self.save_button.pack(side="left")
            ttk.Button(footer, text="重新载入", command=self.load_from_patch).pack(side="left", padx=6)
            ttk.Button(footer, text="撤销上次保存", command=self.revert).pack(side="left")
            ttk.Button(footer, text="复制人设", command=self.copy_text).pack(side="left", padx=6)
            ttk.Button(footer, text="退出", command=self.on_close).pack(side="right")

            self.status = ttk.Label(outer, text="", wraplength=960, justify="left")
            self.status.pack(fill="x", pady=(6, 0))

            root.protocol("WM_DELETE_WINDOW", self.on_close)
            self.refresh_files()
            self.rescan()

        # ── 找 DSH 的补丁文件 ────────────────────────────────────────────────
        @staticmethod
        def same_file(left, right) -> bool:
            try:
                return Path(left).resolve() == Path(right).resolve()
            except (OSError, RuntimeError):
                return str(left) == str(right)

        @staticmethod
        def entry_label(entry: dict) -> str:
            where = Path(entry["path"]).name
            if entry.get("source") == "插件包补丁":
                package = Path(entry["path"]).parent
                where = f"{package.parent.name}/{package.name}"
            count = len(entry.get("presets") or [])
            return f"{entry.get('profile') or '未命名 profile'} · {where} · {count} 个 preset"

        def rescan(self) -> None:
            """Look for DSH's patch files again, keeping the current one selected."""
            entries = [entry for entry in discover_patch_files() if not entry.get("error")]
            if not entries:
                entries = [{
                    "path": self.target.patch_file,
                    "profile": "",
                    "source": "手动指定",
                    "presets": [],
                    "selected_default": None,
                    "error": None,
                }]
            self.patch_entries = entries
            self.file_box.configure(values=[self.entry_label(entry) for entry in entries])
            index = next(
                (i for i, entry in enumerate(entries) if self.same_file(entry["path"], self.target.patch_file)),
                0,
            )
            self.file_box.current(index)
            self.use_entry(entries[index])

        def use_selected_file(self) -> None:
            index = self.file_box.current()
            if 0 <= index < len(self.patch_entries):
                self.use_entry(self.patch_entries[index])

        def use_entry(self, entry: dict) -> None:
            """Switch the file being edited; the preset list follows at once."""
            self.target.patch_file = Path(entry["path"])
            self.path_label.configure(text=str(self.target.patch_file))
            self.load_from_patch()

        def pick_file(self) -> None:
            current = self.target.patch_file
            chosen = filedialog.askopenfilename(
                title="选择 DSH 的 cordis.patch.yml",
                initialdir=str(current.parent) if current.parent.is_dir() else str(Path.home()),
                filetypes=[("YAML", "*.yml *.yaml"), ("所有文件", "*.*")],
            )
            if not chosen:
                return
            self.target.patch_file = Path(chosen)
            saved = save_config({"patchFile": str(self.target.patch_file)})
            self.rescan()
            note = f"，已记住到 {saved.name}" if saved else "（config.json 写不进去，下次还要重选）"
            self.set_status(f"改用 {self.target.patch_file}{note}")

        # ── 新建 / 删除预设 ──────────────────────────────────────────────────
        def new_preset_dialog(self) -> None:
            """Ask for id / name / template, then append a fresh preset block."""
            self.set_status("正在收集可以复制的模板…")
            self.root.update()
            try:
                usable = [template for template in preset_templates() if template.get("plugin_rows")]
            except Exception as error:  # noqa: BLE001 - surfaced to the user
                self.set_status(f"找模板失败：{error}", error=True)
                return
            if not usable:
                self.set_status("没找到模板：这台机器上还没有 preset，也读不到 DSH 自带的 presets。", error=True)
                return

            dialog = tk.Toplevel(self.root)
            dialog.title("新建预设")
            dialog.transient(self.root)
            dialog.resizable(False, False)
            frame = ttk.Frame(dialog, padding=14)
            frame.pack(fill="both", expand=True)

            ttk.Label(frame, text="preset id（小写字母、数字、连字符）：").grid(row=0, column=0, sticky="w")
            id_entry = ttk.Entry(frame, width=30)
            id_entry.grid(row=0, column=1, sticky="we", pady=2)
            id_entry.insert(0, "my-persona")

            ttk.Label(frame, text="显示名：").grid(row=1, column=0, sticky="w")
            name_entry = ttk.Entry(frame, width=30)
            name_entry.grid(row=1, column=1, sticky="we", pady=2)
            name_entry.insert(0, "我的人设")

            ttk.Label(frame, text="说明（可空）：").grid(row=2, column=0, sticky="w")
            desc_entry = ttk.Entry(frame, width=30)
            desc_entry.grid(row=2, column=1, sticky="we", pady=2)

            ttk.Label(frame, text="模板（复制它的插件表）：").grid(row=3, column=0, sticky="w")
            template_box = ttk.Combobox(frame, state="readonly", width=52)
            template_box["values"] = [
                f"{template['id']} · {template.get('plugin_rows')} 行 · "
                f"{'有人设' if template.get('persona') else '无人设'} · {template['source']}"
                for template in usable
            ]
            template_box.current(0)
            template_box.grid(row=3, column=1, sticky="we", pady=2)

            current_text = self.text.get("1.0", "end-1c")
            use_text = tk.BooleanVar(value=bool(current_text.strip()))
            ttk.Checkbutton(frame, variable=use_text, text="用编辑框里的正文作为初始人设").grid(
                row=4, column=0, columnspan=2, sticky="w", pady=(6, 0)
            )
            select_var = tk.BooleanVar(value=False)
            ttk.Checkbutton(frame, variable=select_var, text="创建后设为 DSH 当前使用的 preset").grid(
                row=5, column=0, columnspan=2, sticky="w"
            )
            ttk.Label(
                frame,
                text=f"写入：{self.target.patch_file}",
                foreground="#666",
                wraplength=460,
                justify="left",
            ).grid(row=6, column=0, columnspan=2, sticky="w", pady=(6, 0))

            def submit() -> None:
                preset_id = id_entry.get().strip()
                template = usable[template_box.current()]
                try:
                    result = create_preset(
                        self.target,
                        preset_id,
                        name=name_entry.get().strip() or preset_id,
                        description=desc_entry.get().strip() or None,
                        template=template,
                        text=current_text if use_text.get() else None,
                        select=select_var.get(),
                    )
                except Exception as error:  # noqa: BLE001 - surfaced to the user
                    self.set_status(f"新建失败：{error}", error=True)
                    return
                dialog.destroy()
                self.chosen.pop(self.file_key(), None)
                self.rescan()
                self.set_preset_choice(self.preset_index(result["preset_label"]), remember=True)
                self.load_from_patch()
                extra = "，已设为 DSH 当前 preset" if result["selected_default"] else ""
                self.set_status(
                    f"已新建 preset「{result['preset_label']}」（模板 {result['template']}）{extra}"
                    f"　备份 {result['backup'].name}　DSH 会在 1～2 秒内热加载。"
                )

            buttons = ttk.Frame(frame)
            buttons.grid(row=7, column=0, columnspan=2, sticky="e", pady=(10, 0))
            ttk.Button(buttons, text="创建", command=submit).pack(side="left")
            ttk.Button(buttons, text="取消", command=dialog.destroy).pack(side="left", padx=(6, 0))
            id_entry.focus_set()
            try:
                dialog.grab_set()
            except tk.TclError:
                pass

        def delete_current_preset(self) -> None:
            """Remove the preset on screen, with a confirmation and a backup."""
            index = self.preset_box.current()
            preset = self.presets[index] if 0 <= index < len(self.presets) else None
            label = preset_label(preset) if preset else self.target.preset_id
            if not label:
                self.set_status("这个文件里没有可以删除的 preset。", error=True)
                return
            if not messagebox.askyesno(
                APP_NAME,
                f"从 {self.target.patch_file.name} 删除 preset「{label}」？\n"
                "写入前会自动备份；可以用「撤销上次保存」回滚。",
            ):
                return
            try:
                result = remove_preset(self.target, label)
            except DshRunningError as error:
                messagebox.showwarning("先退出 DSH", str(error), parent=self.root)
                self.set_status("已拦住：DSH 正在运行，先退出再删。", error=True)
                return
            except Exception as error:  # noqa: BLE001 - surfaced to the user
                self.set_status(f"删除失败：{error}", error=True)
                return
            self.chosen.pop(self.file_key(), None)
            self.rescan()
            note = ""
            if result["restored_empty_list"]:
                note += "　文件恢复为空列表 []"
            if result["registry_moved"]:
                note += "　DSH 的选中项已改指其他 preset"
            self.set_status(f"已删除 preset「{result['removed']}」{note}　备份 {result['backup'].name}。")

        def edit_preset_dialog(self) -> None:
            """Edit the selected preset's id / name / description / order."""
            index = self.preset_box.current()
            preset = self.presets[index] if 0 <= index < len(self.presets) else None
            if preset is None or not preset_label(preset):
                self.set_status("这个文件里没有可以编辑的 preset。", error=True)
                return
            old_label = preset_label(preset)
            old_name = preset.get("name") or ""
            old_description = preset.get("description") or ""
            old_order = str(preset.get("order") or "")

            dialog = tk.Toplevel(self.root)
            dialog.title(f"编辑预设信息 — {old_label}")
            dialog.transient(self.root)
            dialog.resizable(False, False)
            frame = ttk.Frame(dialog, padding=14)
            frame.pack(fill="both", expand=True)

            ttk.Label(frame, text="preset id（小写字母、数字、连字符）：").grid(row=0, column=0, sticky="w")
            id_entry = ttk.Entry(frame, width=30)
            id_entry.grid(row=0, column=1, sticky="we", pady=2)
            id_entry.insert(0, old_label)

            ttk.Label(frame, text="显示名：").grid(row=1, column=0, sticky="w")
            name_entry = ttk.Entry(frame, width=30)
            name_entry.grid(row=1, column=1, sticky="we", pady=2)
            name_entry.insert(0, old_name)

            ttk.Label(frame, text="说明：").grid(row=2, column=0, sticky="w")
            desc_entry = ttk.Entry(frame, width=30)
            desc_entry.grid(row=2, column=1, sticky="we", pady=2)
            desc_entry.insert(0, old_description)

            ttk.Label(frame, text="排序（数字，越小越靠前）：").grid(row=3, column=0, sticky="w")
            order_entry = ttk.Entry(frame, width=30)
            order_entry.grid(row=3, column=1, sticky="we", pady=2)
            order_entry.insert(0, old_order)

            ttk.Label(
                frame,
                text="改动会整份校验并自动备份；改 id 时 DSH 的选中项会跟着更新。\n"
                     "留空的说明/排序会被删掉这一项。",
                foreground="#666",
                wraplength=430,
                justify="left",
            ).grid(row=4, column=0, columnspan=2, sticky="w", pady=(6, 0))

            def submit() -> None:
                new_id = id_entry.get().strip()
                name_text = name_entry.get().strip()
                desc_text = desc_entry.get().strip()
                order_text = order_entry.get().strip()
                try:
                    result = edit_preset(
                        self.target,
                        preset_id=new_id if new_id and new_id != old_label else None,
                        name=name_text if name_text != old_name else None,
                        description=desc_text if desc_text != old_description else None,
                        order=order_text if order_text != old_order else None,
                    )
                except DshRunningError as error:
                    messagebox.showwarning("先退出 DSH", str(error), parent=dialog)
                    self.set_status("已拦住：DSH 正在运行，改 id 前请先退出它。", error=True)
                    return
                except Exception as error:  # noqa: BLE001 - surfaced to the user
                    self.set_status(f"修改失败：{error}", error=True)
                    return
                dialog.destroy()
                self.chosen.pop(self.file_key(), None)
                self.rescan()
                self.set_preset_choice(self.preset_index(result["new"]), remember=True)
                self.load_from_patch()
                moved = "　DSH 选中项已跟着更新" if result["selected_moved"] else ""
                sessions = describe_session_fix(result.get("sessions"))
                self.set_status(
                    f"已更新 preset「{result['new']}」"
                    f"{'（原 ' + result['old'] + '）' if result['old'] != result['new'] else ''}{moved}"
                    f"　备份 {result['backup'].name}"
                )
                if sessions and result["old"] != result["new"]:
                    messagebox.showinfo("预设信息已更新", "\n".join(sessions), parent=self.root)

            buttons = ttk.Frame(frame)
            buttons.grid(row=5, column=0, columnspan=2, sticky="e", pady=(10, 0))
            ttk.Button(buttons, text="保存修改", command=submit).pack(side="left")
            ttk.Button(buttons, text="取消", command=dialog.destroy).pack(side="left", padx=(6, 0))
            id_entry.focus_set()
            try:
                dialog.grab_set()
            except tk.TclError:
                pass

        # ── helpers ──────────────────────────────────────────────────────────
        def set_status(self, message: str, error: bool = False) -> None:
            self.status.configure(text=message, foreground="#b23" if error else "#2a6")

        def file_key(self) -> str:
            try:
                return str(self.target.patch_file.resolve()).lower()
            except (OSError, RuntimeError):
                return str(self.target.patch_file).lower()

        def preset_index(self, key: str | None) -> int:
            """Index of a preset named by a config id or a loader row id."""
            if not key:
                return -1
            for index, preset in enumerate(self.presets):
                if key in (preset["preset_id"], preset["row_id"], f"preset-{preset['preset_id']}"):
                    return index
            return -1

        def set_preset_choice(self, index: int, remember: bool = False) -> None:
            """Show one preset of the current file, or clear the box.

            Clearing matters: when the newly opened yml has no preset that can
            be edited, a leftover name from the previous file would be a lie.
            """
            if 0 <= index < len(self.presets):
                preset = self.presets[index]
                self.preset_box.current(index)
                self.target.preset_id = preset["preset_id"] or preset["row_id"]
                self.target.preset_row_id = preset["row_id"]
            else:
                self.preset_box.set("")
                self.target.preset_id = None
                self.target.preset_row_id = None
            if remember:
                if self.target.preset_id:
                    self.chosen[self.file_key()] = self.target.preset_id
                else:
                    self.chosen.pop(self.file_key(), None)

        def refresh_presets(self, presets: list[dict]) -> None:
            """Reload the dropdown for the file on screen, then restore its own pick."""
            self.presets = presets
            self.preset_box.configure(
                values=[
                    f"{preset_label(preset)}（{'有人设' if preset.get('persona_rows') else '无人设'}"
                    f"，{preset.get('persona_length', 0)} 字符）"
                    for preset in presets
                ]
            )
            # Each file remembers the preset picked in it; a file never opened
            # before starts from auto-detection instead of the last file's pick.
            self.set_preset_choice(self.preset_index(self.chosen.get(self.file_key())))

        def on_preset_selected(self) -> None:
            self.set_preset_choice(self.preset_box.current(), remember=True)
            self.load_from_patch()

        def refresh_files(self) -> None:
            self.file_items = list_persona_files(self.target.personas_dir)
            self.file_list.delete(0, "end")
            for item in self.file_items:
                self.file_list.insert("end", f"{item['name']}  ({item['bytes'] / 1024:.1f}KB)")
            if not self.file_items:
                self.file_list.insert("end", "（人设目录里还没有 .md/.txt）")

        def on_modified(self, _event=None) -> None:
            if self.text.edit_modified():
                dirty = self.text.get("1.0", "end-1c") != self.loaded_text
                preset = self.target.preset_id or "自动"
                self.root.title(f"{'* ' if dirty else ''}{APP_NAME} {APP_VERSION} — preset {preset}")
                self.text.edit_modified(False)

        # ── actions ──────────────────────────────────────────────────────────
        def load_from_patch(self) -> None:
            lines: list[str] | None = None
            try:
                if not self.target.patch_file.is_file():
                    self.refresh_presets([])
                    raise FileNotFoundError(
                        f"没有找到 {self.target.patch_file}；点「重新扫描」或「选择 yml…」指定一个文件"
                    )
                raw = self.target.patch_file.read_text(encoding="utf-8", errors="replace")
                lines = split_text(raw)["lines"]
                # summarize_patch adds the persona row / text length each label shows.
                self.refresh_presets(summarize_patch(self.target.patch_file)["presets"])
                info = inspect_persona(lines, self.target)
            except Exception as error:  # noqa: BLE001 - surfaced to the user
                self.text.delete("1.0", "end")
                self.loaded_text = ""
                self.on_modified()
                # Show which preset this file actually resolves to, and clear the
                # box when it has none, so the dropdown never describes the
                # previous file.
                if lines is not None:
                    resolved = resolve_preset(lines, self.target)
                    self.set_preset_choice(self.preset_index(preset_label(resolved) if resolved else None))
                self.set_status(f"读取失败：{error}", error=True)
                return
            resolved = info["preset"]
            self.set_preset_choice(self.preset_index(resolved["preset_id"] or resolved["row_id"]))
            text = info["text"]
            self.text.delete("1.0", "end")
            self.text.insert("1.0", text)
            self.loaded_text = text
            self.text.edit_modified(False)
            self.on_modified()
            self.set_status(
                f"正在编辑 preset「{info['preset_label']}」（{info['resolved_by']}）"
                f"　persona 行 {info['row_id']}　字段 {info['field']}　{len(text)} 字符"
            )

        def load_file(self) -> None:
            selection = self.file_list.curselection()
            index = selection[0] if selection else -1
            if index < 0 or index >= len(self.file_items):
                self.set_status("先在右边选一个人设文件。", error=True)
                return
            item = self.file_items[index]
            try:
                text = canonical_text(item["path"].read_text(encoding="utf-8"))
            except OSError as error:
                self.set_status(f"读取失败：{error}", error=True)
                return
            self.text.delete("1.0", "end")
            self.text.insert("1.0", text)
            self.on_modified()
            self.set_status(f"已载入 {item['name']}（{len(text)} 字符）——点「保存到预设」才会写入文件。")

        def save(self) -> None:
            text = self.text.get("1.0", "end-1c")
            try:
                result = write_persona(self.target, text)
            except Exception as error:  # noqa: BLE001 - surfaced to the user
                self.set_status(f"保存失败：{error}", error=True)
                return
            self.loaded_text = canonical_text(text)
            self.text.delete("1.0", "end")
            self.text.insert("1.0", self.loaded_text)
            self.text.edit_modified(False)
            self.on_modified()
            extra = "（已自动清理末尾空行与行尾空格）" if result["normalized"] else ""
            self.set_status(
                f"已保存 preset「{result['preset_label']}」：{result['previous_length']} → {result['length']} 字符{extra}"
                f"　persona 行 {result['row_id']}　字段 {result['field']}"
                f"　备份：{result['backup'].name}"
                "　DSH 会在 1～2 秒内热加载；已存在的会话需要新开一个对话才用上新人设。"
            )

        def save_as(self) -> None:
            name = self.save_name.get().strip()
            if name == "":
                self.set_status("先在左边填一个文件名，例如 我的新人设.md", error=True)
                return
            if not name.lower().endswith((".md", ".markdown", ".txt", ".text", ".persona")):
                name += ".md"
            path = self.target.personas_dir / name
            try:
                self.target.personas_dir.mkdir(parents=True, exist_ok=True)
                text = canonical_text(self.text.get("1.0", "end-1c"))
                path.write_text(text + "\n", encoding="utf-8")
            except OSError as error:
                self.set_status(f"另存为失败：{error}", error=True)
                return
            self.save_name.delete(0, "end")
            self.refresh_files()
            self.set_status(f"已写出 {path}（{len(text)} 字符）。")

        def revert(self) -> None:
            if not messagebox.askyesno(APP_NAME, "用最近一次备份覆盖当前的 cordis.patch.yml？"):
                return
            try:
                result = revert_patch(self.target)
            except Exception as error:  # noqa: BLE001 - surfaced to the user
                self.set_status(f"回滚失败：{error}", error=True)
                return
            self.load_from_patch()
            self.set_status(f"已回滚到 {result['restored_from'].name}。")

        def copy_text(self) -> None:
            self.root.clipboard_clear()
            self.root.clipboard_append(self.text.get("1.0", "end-1c"))
            self.set_status("已复制到剪贴板。")

        def open_folder(self) -> None:
            try:
                self.target.personas_dir.mkdir(parents=True, exist_ok=True)
                os.startfile(self.target.personas_dir)  # noqa: S606 - intentional, Windows shell
            except OSError as error:
                self.set_status(f"打开文件夹失败：{error}", error=True)

        def on_close(self) -> None:
            current = self.text.get("1.0", "end-1c")
            if current != self.loaded_text and not messagebox.askyesno(
                APP_NAME, "人设还没保存，确定关闭吗？"
            ):
                return
            self.root.destroy()

    root = tk.Tk()
    try:
        root.call("tk", "scaling", 1.2)
    except Exception:
        pass
    if headless:
        root.withdraw()
    editor = Editor(root)
    if headless:
        root.update()  # build every widget and run the first data load
        try:
            if probe is not None:
                probe(editor)
        finally:
            root.after(150, root.destroy)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
