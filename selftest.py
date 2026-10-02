#!/usr/bin/env python3
"""Smoke tests for the persona editor: discovery, parsing, writing, GUI.

Everything runs against copies in a temp directory; the real profile patch is
only ever read. Usage: python selftest.py

The synthetic patch below is the point of these tests: the editor must find
DSH's own yml, pick the right preset and the right persona row on a machine it
has never seen, including a config.json copied from someone else's folder.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import persona_editor  # noqa: E402
from persona_editor import (  # noqa: E402
    Target,
    canonical_text,
    discover_patch_files,
    dsh_homes,
    inspect_persona,
    list_persona_files,
    list_presets,
    preset_label,
    read_persona,
    resolve_preset,
    revert_patch,
    split_text,
    summarize_patch,
    write_persona,
)

failures = 0


def check(label: str, condition: bool, detail: str = "") -> None:
    global failures
    ok = bool(condition)
    if not ok:
        failures += 1
    print(f"{'PASS' if ok else 'FAIL'}  {label}" + (f"  — {detail}" if detail else ""))


SYNTHETIC = """\
# synthetic patch for the self-test
- id: agent-preset-registry
  name: "@deepseek-ai/dsh-agent-preset-registry"
  config:
    default: standard
    selectedDefault: mine
- insert:
    - id: preset-standard
      name: '@deepseek-ai/dsh-agent-preset'
      config:
        id: standard
        name: Standard
        order: 1
        plugins:
          - id: tool-bash
            name: '@deepseek-ai/dsh-tool-bash'
          - id: persona
            name: '@deepseek-ai/dsh-persona'
            config:
              prefix: |-
                标准人设第一行
                标准人设第二行
    - id: preset-mine
      name: '@deepseek-ai/dsh-agent-preset'
      config:
        id: mine
        name: 我的人设
        description: 测试用
        order: 2
        plugins:
          - id: tool-bash
            name: '@deepseek-ai/dsh-tool-bash'
          - id: persona
            name: '@deepseek-ai/dsh-persona'
            config:
              suffix: Your working directory is {{cwd}}.
              prefix: |-
                我的人设第一行
                我的人设第二行
    - id: preset-legacy
      name: '@deepseek-ai/dsh-agent-preset'
      config:
        id: legacy
        name: 旧字段
        plugins:
          - id: persona
            name: dsh-persona
            config:
              text: |-
                旧版 text 字段的人设
    - id: preset-empty
      name: '@deepseek-ai/dsh-agent-preset'
      config:
        id: empty
        name: 没有正文
        plugins:
          - id: persona
            name: '@deepseek-ai/dsh-persona'
"""

work = Path(tempfile.mkdtemp(prefix="persona-editor-test-"))
patch = work / "cordis.patch.yml"
patch.write_text(SYNTHETIC, encoding="utf-8")
original_bytes = patch.read_bytes()
personas_dir = work / "personas"
personas_dir.mkdir()
(personas_dir / "样例.md").write_text("样例人设\n第二行\n", encoding="utf-8")

# ── 1. parsing and auto-detection ────────────────────────────────────────────
lines = split_text(patch.read_text(encoding="utf-8"))["lines"]
summary = summarize_patch(patch)
presets = summary["presets"]
check(
    "找到全部 preset",
    [preset_label(preset) for preset in presets] == ["standard", "mine", "legacy", "empty"],
    str([preset_label(preset) for preset in presets]),
)
check("读到 registry 的选中项", summary["selected_default"] == "mine", str(summary["selected_default"]))
check(
    "persona 行按包名识别",
    [preset.get("persona_rows") for preset in presets] == [["persona"], ["persona"], ["persona"], ["persona"]],
    str([preset.get("persona_rows") for preset in presets]),
)

target = Target(patch_file=patch, personas_dir=personas_dir)
auto = resolve_preset(lines, target)
check("没有配置时选中 DSH 当前 preset", preset_label(auto) == "mine", str(preset_label(auto)))
info = inspect_persona(lines, target)
check("识别人设字段 prefix", info["field"] == "prefix" and info["row_id"] == "persona", str(info["field"]))
check("读到当前人设", info["text"] == "我的人设第一行\n我的人设第二行", repr(info["text"]))

legacy = Target(patch_file=patch, personas_dir=personas_dir, preset_id="legacy")
legacy_info = inspect_persona(lines, legacy)
check("旧版 text 字段也能读", legacy_info["field"] == "text", str(legacy_info["field"]))
check("旧版 text 字段内容正确", legacy_info["text"] == "旧版 text 字段的人设", repr(legacy_info["text"]))

stale = Target(patch_file=patch, personas_dir=personas_dir, preset_id="这台机器上没有", preset_row_id="preset-别的")
stale_preset = resolve_preset(lines, stale)
check("配置里不存在的 preset 退回自动选择", preset_label(stale_preset) == "mine", str(preset_label(stale_preset)))

# ── 2. canonical form ────────────────────────────────────────────────────────
check(
    "粘贴文本被规范化",
    canonical_text("  \n第一行   \r\n第二行\n\n\n") == "第一行\n第二行",
    repr(canonical_text("  \n第一行   \r\n第二行\n\n\n")),
)

# ── 3. write, then read back ─────────────────────────────────────────────────
tricky = "# 开头像注释\n- 开头像列表\nkey: value\n\n中间空行\n  两个前导空格"
result = write_persona(target, tricky)
check("写入成功", result["ok"] and result["length"] == len(tricky), f"{result['previous_length']} → {result['length']}")
check("写入报告带 preset 与行", result["preset_label"] == "mine" and result["row_id"] == "persona", str(result))
check("备份已生成", Path(result["backup"]).is_file())
after_lines = split_text(patch.read_text(encoding="utf-8"))["lines"]
check("回读与输入一致", read_persona(after_lines, target) == tricky)


def slice_index(lines: list[str], needle: str) -> int:
    return next(i for i, line in enumerate(lines) if line.strip() == needle)


# ── 4. only the edited preset's persona block changed ──────────────────────
mine_before, mine_after = slice_index(lines, "id: mine"), slice_index(after_lines, "id: mine")
legacy_before, legacy_after = slice_index(lines, "id: legacy"), slice_index(after_lines, "id: legacy")
check("编辑处之前逐行不变", lines[:mine_before] == after_lines[:mine_after])
check("编辑处之后逐行不变", lines[legacy_before:] == after_lines[legacy_after:])
check(
    "其他 preset 的人设未被带动",
    "标准人设第一行" in "\n".join(after_lines) and "旧版 text 字段的人设" in "\n".join(after_lines),
)

# ── 5. no-op write is byte-identical ────────────────────────────────────────
current = patch.read_text(encoding="utf-8")
same = write_persona(target, read_persona(split_text(current)["lines"], target))
check("原样写回零漂移", patch.read_text(encoding="utf-8") == current, str(same["length"]))

# ── 6. pasted text with trailing newline and spaces ─────────────────────────
pasted = write_persona(target, "带末尾换行   \n第二行。\n\n")
check("末尾换行/行尾空格被接受", pasted["ok"] and pasted["normalized"])
check(
    "写入的是规范化结果",
    read_persona(split_text(patch.read_text(encoding="utf-8"))["lines"], target) == "带末尾换行\n第二行。",
)

# ── 7. legacy `text` row keeps its own key ──────────────────────────────────
prefix_before = patch.read_text(encoding="utf-8").count("prefix:")
legacy_written = write_persona(legacy, "换掉的旧版人设")
legacy_text = patch.read_text(encoding="utf-8")
check("旧版行写回 text 字段", legacy_written["field"] == "text" and "换掉的旧版人设" in legacy_text)
check("旧版行没有多出 prefix", legacy_text.count("prefix:") == prefix_before, f"{prefix_before} → {legacy_text.count('prefix:')}")

# ── 8. a persona row without text gets the key inserted ────────────────────
empty_target = Target(patch_file=patch, personas_dir=personas_dir, preset_id="empty")
inserted = write_persona(empty_target, "新插入的人设")
check("空 persona 行插入 prefix", inserted["style"] == "插入 prefix" and inserted["field"] == "prefix", str(inserted["style"]))
check("插入后可回读", read_persona(split_text(patch.read_text(encoding="utf-8"))["lines"], empty_target) == "新插入的人设")

try:
    import yaml

    document = yaml.safe_load(patch.read_text(encoding="utf-8"))
    presets_yaml = {
        entry["config"]["id"]: entry["config"]
        for entry in next(item for item in document if "insert" in item)["insert"]
    }
    check("整份文件仍是合法 YAML", isinstance(document, list), f"{len(document)} 个顶层条目")
    check(
        "YAML 里读到写入的人设",
        presets_yaml["empty"]["plugins"][0]["config"]["prefix"].strip() == "新插入的人设",
        repr(presets_yaml["empty"]["plugins"][0]["config"].get("prefix")),
    )
except ImportError:  # pragma: no cover - PyYAML is a dev-only convenience
    check("YAML 校验（跳过：没有 PyYAML）", True)

# ── 9. persona files are listed ─────────────────────────────────────────────
files = list_persona_files(personas_dir)
check("列出人设文件", [item["name"] for item in files] == ["样例.md"], str([item["name"] for item in files]))

# ── 10. revert pops one save at a time, back to the original bytes ─────────
replay = work / "replay.patch.yml"
replay.write_text(SYNTHETIC, encoding="utf-8")
replay_original = replay.read_bytes()
replay_target = Target(replay, work / "replay-personas")
write_persona(replay_target, "第一次保存")
first_bytes = replay.read_bytes()
check("第一次保存改变了文件", first_bytes != replay_original)
write_persona(replay_target, "第二次保存")
revert_patch(replay_target)
check("回滚一次回到上一次保存", replay.read_bytes() == first_bytes)
revert_patch(replay_target)
check("再回滚一次回到原始文件", replay.read_bytes() == replay_original)
try:
    revert_patch(replay_target)
    check("备份用完时给出提示", False, "没有抛错")
except FileNotFoundError as error:
    check("备份用完时给出提示", "备份" in str(error), str(error))

# ── 11. a CRLF file keeps its line endings ─────────────────────────────────
crlf = work / "crlf.patch.yml"
crlf.write_bytes(SYNTHETIC.replace("\n", "\r\n").encode("utf-8"))
crlf_target = Target(crlf, work / "crlf-personas")
write_persona(crlf_target, "CRLF 人设")
crlf_bytes = crlf.read_bytes()
check("CRLF 文件保持 CRLF", b"\r\n" in crlf_bytes and b"\n" not in crlf_bytes.replace(b"\r\n", b""))
check(
    "CRLF 文件仍可回读",
    read_persona(split_text(crlf.read_text(encoding="utf-8"))["lines"], crlf_target) == "CRLF 人设",
)

# ── 11. a preset without a persona row reports clearly ─────────────────────
bare = work / "bare.patch.yml"
bare.write_text("- insert:\n    - id: preset-bare\n      name: '@deepseek-ai/dsh-agent-preset'\n      config:\n        id: bare\n        plugins:\n          - id: tool-bash\n            name: '@deepseek-ai/dsh-tool-bash'\n", encoding="utf-8")
try:
    read_persona(split_text(bare.read_text(encoding="utf-8"))["lines"], Target(bare, personas_dir))
    check("没有 persona 行时给出提示", False, "没有抛错")
except LookupError as error:
    check("没有 persona 行时给出提示", "persona" in str(error), str(error))

empty_file = work / "empty.patch.yml"
empty_file.write_text("[]\n", encoding="utf-8")
try:
    read_persona(split_text(empty_file.read_text(encoding="utf-8"))["lines"], Target(empty_file, personas_dir))
    check("空补丁文件给出提示", False, "没有抛错")
except LookupError as error:
    check("空补丁文件给出提示", "preset" in str(error), str(error))

# ── 12. discovery on a synthetic machine ───────────────────────────────────
home = work / "fake-home"
(home / "profiles" / "desktop").mkdir(parents=True)
(home / "profiles" / "web").mkdir(parents=True)
bundle = home / "profiles" / "desktop" / "node_modules" / "@local" / "dsh-my-preset"
bundle.mkdir(parents=True)
shutil.copyfile(patch, home / "profiles" / "desktop" / "cordis.patch.yml")
(home / "profiles" / "web" / "cordis.patch.yml").write_text("[]\n", encoding="utf-8")
(bundle / "cordis.patch.yml").write_text(SYNTHETIC, encoding="utf-8")

previous_env = {name: os.environ.get(name) for name in ("DSH_HOME", "DSH_PROFILE", "DSH_PROFILE_DIR")}
os.environ["DSH_HOME"] = str(home)
os.environ.pop("DSH_PROFILE", None)
os.environ.pop("DSH_PROFILE_DIR", None)
try:
    synthetic_desktop = home / "profiles" / "desktop" / "cordis.patch.yml"
    synthetic_web = home / "profiles" / "web" / "cordis.patch.yml"

    check("找到合成的 DSH 目录", dsh_homes()[0] == home.resolve(), str(dsh_homes()[0]))
    found = discover_patch_files()
    found_paths = [entry["path"] for entry in found]
    check("发现 profile 补丁", synthetic_desktop in found_paths)
    check("发现空 profile 的补丁", synthetic_web in found_paths)
    check("发现插件包补丁", bundle / "cordis.patch.yml" in found_paths)
    check(
        "有人的 preset 排在无人前面",
        found_paths.index(synthetic_desktop) < found_paths.index(synthetic_web),
        str([(entry["profile"], len(entry.get("presets") or [])) for entry in found[:4]]),
    )
    check(
        "插件包条目来源正确",
        next(entry["source"] for entry in found if entry["path"] == bundle / "cordis.patch.yml") == "插件包补丁",
    )

    os.environ["DSH_PROFILE"] = "web"  # the environment's profile wins the ordering
    preferred = discover_patch_files()
    preferred_paths = [entry["path"] for entry in preferred]
    check(
        "DSH_PROFILE 的 profile 排在前面",
        preferred_paths.index(synthetic_web) < preferred_paths.index(synthetic_desktop),
        str([entry["profile"] for entry in preferred[:4]]),
    )

    # ── 13. GUI: switching the yml updates the preset box at once ─────────
    gui_target = Target(synthetic_desktop, personas_dir)
    observed: dict = {}

    def drive(editor) -> None:
        def use(path: Path) -> None:
            index = next(
                i for i, entry in enumerate(editor.patch_entries) if editor.same_file(entry["path"], path)
            )
            editor.file_box.current(index)
            editor.use_selected_file()

        observed["start"] = editor.preset_box.get()
        observed["labels"] = list(editor.preset_box.cget("values"))
        use(synthetic_web)  # a profile patch with no preset at all
        observed["web_box"] = editor.preset_box.get()
        observed["web_labels"] = list(editor.preset_box.cget("values"))
        observed["web_path"] = editor.path_label.cget("text")
        observed["web_target"] = editor.target.preset_id
        use(synthetic_desktop)  # back to the file with four presets
        observed["back_box"] = editor.preset_box.get()
        observed["back_labels"] = list(editor.preset_box.cget("values"))
        editor.preset_box.current(2)  # an explicit pick: "legacy"
        editor.on_preset_selected()
        observed["picked"] = editor.target.preset_id
        use(synthetic_web)
        use(synthetic_desktop)
        observed["remembered"] = editor.preset_box.get()

    try:
        code = persona_editor.run_gui(gui_target, headless=True, probe=drive)
        check("GUI 构建通过（隐藏窗口）", code == 0)
    except Exception as error:  # noqa: BLE001 - reported
        check("GUI 构建通过（隐藏窗口）", False, str(error))
    check("打开时选中带人设的 preset", str(observed.get("start", "")).startswith("mine"), str(observed.get("start")))
    check("切到没有 preset 的文件会清空 preset 框", observed.get("web_box") == "", repr(observed.get("web_box")))
    check("切到没有 preset 的文件会清空下拉项", observed.get("web_labels") == [], repr(observed.get("web_labels")))
    check("切文件后写入目标同步更新", "web" in str(observed.get("web_path")), str(observed.get("web_path")))
    check("切文件后不再挂念旧 preset", not observed.get("web_target"), str(observed.get("web_target")))
    check("切回来立刻恢复 preset 列表", observed.get("back_labels") == observed.get("labels"), str(observed.get("back_labels")))
    check(
        "preset 标签写明了人设状态与字数",
        (observed.get("back_labels") or [""])[1] == "mine（有人设，10 字符）",
        str(observed.get("back_labels")),
    )
    check("切回来立刻恢复选中的 preset", str(observed.get("back_box", "")).startswith("mine"), str(observed.get("back_box")))
    check("手动选的 preset 被记住", observed.get("picked") == "legacy", str(observed.get("picked")))
    check("再切回来用的是记住的那个", str(observed.get("remembered", "")).startswith("legacy"), str(observed.get("remembered")))
finally:
    for name, value in previous_env.items():
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value

# ── 14. this machine's own file, read-only ─────────────────────────────────
real = Path.home() / ".dsh" / "profiles" / "desktop" / "cordis.patch.yml"
if real.is_file():
    real_info = inspect_persona(split_text(real.read_text(encoding="utf-8"))["lines"], Target(real, personas_dir))
    check(
        "本机真实文件可自动识别",
        len(real_info["text"]) > 50 and real_info["row_id"] == "persona",
        f"preset {real_info['preset_label']} / {len(real_info['text'])} 字符",
    )
else:
    print(f"SKIP  本机没有 {real}")

# ── 15. config.json: honored here, ignored when it came from elsewhere ─────
import argparse  # noqa: E402

fake_app = work / "fake-app"
fake_app.mkdir()
empty_args = argparse.Namespace(patch=None, personas=None, preset=None, persona_row=None)
real_app_dir = persona_editor.app_dir
persona_editor.app_dir = lambda: fake_app
try:
    (fake_app / "config.json").write_text(
        json.dumps(
            {
                "patchFile": "Z:/没有这个盘/cordis.patch.yml",
                "personasDir": "Z:/没有这个盘/personas",
                "presetId": "ghost",
                "personaRowId": "ghost-row",
            }
        ),
        encoding="utf-8",
    )
    stale = Target.resolve(empty_args)
    check("指向不存在文件的 patchFile 被忽略", str(stale.patch_file) != "Z:\\没有这个盘\\cordis.patch.yml", str(stale.patch_file))
    check("指向不存在目录的 personasDir 被忽略", stale.personas_dir == fake_app / "personas", str(stale.personas_dir))
    ghost = resolve_preset(lines, Target(patch, personas_dir, preset_id="ghost", persona_row_id="ghost-row"))
    check("不存在的 preset 偏好不影响编辑", preset_label(ghost) == "mine", str(preset_label(ghost)))

    (fake_app / "config.json").write_text(
        json.dumps({"patchFile": str(patch), "personasDir": str(personas_dir)}), encoding="utf-8"
    )
    honored = Target.resolve(empty_args)
    check("本机 config.json 生效", honored.patch_file == patch and honored.personas_dir == personas_dir, str(honored.patch_file))
finally:
    persona_editor.app_dir = real_app_dir

# ── 16. 新建 / 删除预设 ─────────────────────────────────────────────────────
from persona_editor import (  # noqa: E402
    create_preset,
    ensure_persona_row,
    find_template,
    indent_block,
    insert_block_into_lines,
    list_asar_members,
    pick_template,
    preset_templates,
    read_asar_member,
    remove_preset,
    set_config_scalar,
    set_row_id,
    template_block_lines,
    templates_from_lines,
)

PERSONA_TEXT = "新建的人设第一行\n新建的人设第二行"

# a synthetic template source: the synthetic patch's own "mine" preset
mine_template = templates_from_lines(lines, "合成", "synthetic.yml")[1]
check("模板条目带插件行与人设信息", mine_template["id"] == "mine" and mine_template["persona"] and mine_template["plugin_rows"] == 2, str(mine_template["id"]))
check("自动挑模板挑中有人设的", pick_template([mine_template])["id"] == "mine")
block = template_block_lines(lines, mine_template["preset"])
check("模板块以 - insert: 开头", block[0] == "- insert:" and block[1].strip().startswith("- id: preset-mine"), block[1])
check("顶层行也能规范成嵌套块", template_block_lines(["- id: preset-x", "  config:", "    id: x"], {"start": 0, "end": 3})[1] == "    - id: preset-x")
check("缩进平移保持块内相对缩进", indent_block(["- id: a", "  config:", "    id: b"], 4) == ["    - id: a", "      config:", "        id: b"])
check("配置标量替换在正确缩进", any(line.strip() == "id: fresh" for line in set_config_scalar(block, "id", "fresh")), "id: fresh")
check("加载行 id 被改名", set_row_id(block, "preset-fresh")[1].strip() == "- id: preset-fresh")

# create into a copy of the synthetic patch
fresh_file = work / "fresh.patch.yml"
shutil.copyfile(patch, fresh_file)
fresh_original = fresh_file.read_bytes()
fresh_target = Target(fresh_file, work / "fresh-personas")
created = create_preset(fresh_target, "fresh-star", name="新星", description="自测用", template=mine_template, text=PERSONA_TEXT)
fresh_lines = split_text(fresh_file.read_text(encoding="utf-8"))["lines"]
check("新建 preset 成功", created["ok"] and created["preset_label"] == "fresh-star", str(created["preset_label"]))
check("新建是追加写入", fresh_file.read_bytes().decode("utf-8").startswith(fresh_original.decode("utf-8")))
check("新建的 preset 立刻可编辑", read_persona(fresh_lines, Target(fresh_file, personas_dir, preset_id="fresh-star")) == PERSONA_TEXT)
fresh_preset = next(p for p in list_presets(fresh_lines) if p["row_id"] == "preset-fresh-star")
check("加载行 id 已改名", fresh_preset["row_id"] == "preset-fresh-star" and fresh_preset["preset_id"] == "fresh-star")
check("原有 preset 全部保留", [preset_label(p) for p in list_presets(fresh_lines)][:4] == ["standard", "mine", "legacy", "empty"])
check("新建报告了模板来源", "mine" in str(created["template"]), str(created["template"]))
check("新建留了备份", Path(created["backup"]).is_file())

# the created file must still be valid YAML with both presets
try:
    import yaml as yaml_module

    fresh_doc = yaml_module.safe_load(fresh_file.read_text(encoding="utf-8"))
    fresh_ids = [
        entry["config"]["id"]
        for item in fresh_doc
        if isinstance(item, dict)
        for entry in (item.get("insert") or [])
        if isinstance(entry, dict) and "config" in entry
    ]
    check("新建后整份文件仍是合法 YAML", isinstance(fresh_doc, list), str(fresh_ids))
    check("YAML 里读到新 preset", fresh_ids[-1] == "fresh-star", str(fresh_ids))
except ImportError:  # pragma: no cover
    check("YAML 校验（跳过：没有 PyYAML）", True)

# refusals
for bad_id, expect in (("Bad_ID", "小写字母"), ("mine", "已经被"), ("standard", "已经被")):
    try:
        create_preset(fresh_target, bad_id, template=mine_template, text="x")
        check(f"拒绝重复/非法 id {bad_id}", False, "没有抛错")
    except ValueError as error:
        check(f"拒绝重复/非法 id {bad_id}", expect in str(error), str(error)[:60])

# a template without a persona row gains one
bare_template = templates_from_lines(split_text(bare.read_text(encoding="utf-8"))["lines"], "合成", "bare.yml")[0]
no_persona_file = work / "no-persona.patch.yml"
shutil.copyfile(bare, no_persona_file)
created2 = create_preset(Target(no_persona_file, work / "np-personas"), "from-bare", template=bare_template, text=PERSONA_TEXT)
check("无人设的模板补上了 persona 行", created2["ok"] and created2["field"] == "prefix", str(created2["field"]))
check(
    "补出的 persona 行可回读",
    read_persona(split_text(no_persona_file.read_text(encoding="utf-8"))["lines"], Target(no_persona_file, personas_dir, preset_id="from-bare")) == PERSONA_TEXT,
)

# creation into a bare `[]` file, then removal restores the exact bytes
empty_copy = work / "empty-copy.patch.yml"
shutil.copyfile(empty_file, empty_copy)
empty_original = empty_copy.read_bytes()
empty_created = create_preset(Target(empty_copy, work / "e-personas"), "fresh-star", template=mine_template, text=PERSONA_TEXT)
check("空列表文件被填上", empty_created["filled_empty_list"], str(empty_created["filled_empty_list"]))
check(
    "空列表文件里新 preset 可读",
    read_persona(split_text(empty_copy.read_text(encoding="utf-8"))["lines"], Target(empty_copy, personas_dir, preset_id="fresh-star")) == PERSONA_TEXT,
)
remove_preset(Target(empty_copy, work / "e-personas"), "fresh-star")
check("删除后恢复为原始字节", empty_copy.read_bytes() == empty_original)

# select=True rewrites only the selectedDefault line; removal moves it back
select_copy = work / "select.patch.yml"
shutil.copyfile(patch, select_copy)
select_original = select_copy.read_bytes()
select_target = Target(select_copy, work / "s-personas")
create_preset(select_target, "fresh-star", template=mine_template, text=PERSONA_TEXT, select=True)
select_text = select_copy.read_text(encoding="utf-8")
check("选中项指向新 preset", "selectedDefault: fresh-star" in select_text)
differences = [
    (a, b)
    for a, b in zip(select_original.decode("utf-8").splitlines(), select_text.splitlines())
    if a != b
]
check(
    "选中之外没有其他改动",
    len(differences) == 1 and "selectedDefault" in differences[0][0],
    str(differences),
)
removed = remove_preset(select_target, "fresh-star")
select_after = select_copy.read_text(encoding="utf-8")
check(
    "删除时选中项落到注册行自己的 default",
    removed["registry_moved"] and "selectedDefault: standard" in select_after,
    str(removed.get("registry_moved")),
)
select_diffs = [
    (a, b)
    for a, b in zip(select_original.decode("utf-8").splitlines(), select_after.splitlines())
    if a != b
]
check(
    "创建+选中+删除只差选中项一行",
    len(select_diffs) == 1 and "selectedDefault" in select_diffs[0][0] and "selectedDefault" in select_diffs[0][1],
    str(select_diffs),
)

# removing one preset out of a shared insert block keeps its siblings
shared_copy = work / "shared.patch.yml"
shutil.copyfile(patch, shared_copy)
shared_target = Target(shared_copy, work / "sh-personas")
removed2 = remove_preset(shared_target, "legacy")
shared_lines = split_text(shared_copy.read_text(encoding="utf-8"))["lines"]
check("共用块里只删一行", removed2["ok"] and not removed2["dropped_insert_block"], str(removed2["dropped_insert_block"]))
check(
    "删除后其他 preset 完好",
    [preset_label(p) for p in list_presets(shared_lines)] == ["standard", "mine", "empty"],
    str([preset_label(p) for p in list_presets(shared_lines)]),
)
check("删除报告剩余 preset", removed2["remaining"] == ["standard", "mine", "empty"], str(removed2["remaining"]))
try:
    remove_preset(shared_target, "legacy")
    check("删除不存在的 preset 给出提示", False, "没有抛错")
except LookupError as error:
    check("删除不存在的 preset 给出提示", "legacy" in str(error), str(error)[:50])

# create then remove is a byte-exact round trip on the synthetic file
round_file = work / "round.patch.yml"
shutil.copyfile(patch, round_file)
round_original = round_file.read_bytes()
round_target = Target(round_file, work / "r-personas")
create_preset(round_target, "temp-one", template=mine_template, text=PERSONA_TEXT)
remove_preset(round_target, "temp-one")
check("创建再删除字节还原", round_file.read_bytes() == round_original)

# a synthetic asar archive can be read back
try:
    member = "dsh/node_modules/@deepseek-ai/dsh-web-app/presets/demo.patch.yml"
    payload = "- insert:\n    - id: preset-demo\n".encode("utf-8")
    index = {"files": {}}
    node = index["files"]
    for part in member.split("/")[:-1]:
        node = node.setdefault(part, {"files": {}})["files"]
    node[member.split("/")[-1]] = {"offset": "0", "size": str(len(payload))}
    header = b"\x04\x00\x00\x00" + b"\x00\x00\x00\x00" + b"\x00\x00\x00\x00"
    index_bytes = json.dumps(index).encode("utf-8")
    fake_asar = work / "fake.app.asar"
    fake_asar.write_bytes(header + len(index_bytes).to_bytes(4, "little") + index_bytes + payload)
    check("asar 里能列出成员", list_asar_members(fake_asar, "dsh/node_modules/@deepseek-ai/dsh-web-app/presets") == [member])
    check("asar 里能读出内容", read_asar_member(fake_asar, member) == payload)
except Exception as error:  # noqa: BLE001 - reported
    check("asar 读取", False, str(error))

# find_template by preset id and by file path; full discovery runs read-only
check("找不到的模板 id 返回 None", find_template("绝对没有的模板id") is None)
check("按文件路径找模板", find_template(str(bare)) is not None)
if preset_templates():
    check("按 preset id 找模板", find_template(preset_templates()[0]["id"]) is not None, str(preset_templates()[0]["id"]))
else:
    check("按 preset id 找模板（跳过：本机没有模板）", True)
discovery_baseline = patch.read_bytes()
preset_templates()
check("模板发现只读不改", patch.read_bytes() == discovery_baseline)

# ── 17. 编辑 preset 的 id / 显示名 / 说明 / 排序 ─────────────────────────────
from persona_editor import edit_preset, registry_selected  # noqa: E402

# rename the auto-selected preset ("mine"): id + row id + registry follow,
# and renaming it back restores the original bytes exactly
meta_file = work / "meta.patch.yml"
shutil.copyfile(patch, meta_file)
meta_original = meta_file.read_bytes()
meta_target = Target(meta_file, work / "m-personas")

renamed = edit_preset(meta_target, preset_id="renamed-one")
meta_lines = split_text(meta_file.read_text(encoding="utf-8"))["lines"]
check("改 id 后 preset 换了名字", renamed["ok"] and renamed["new"] == "renamed-one", str(renamed["new"]))
check("加载行 id 一起改", next(p["row_id"] for p in list_presets(meta_lines) if p["preset_id"] == "renamed-one") == "preset-renamed-one")
check("注册行选中项跟着改", registry_selected(meta_lines) == "renamed-one", str(registry_selected(meta_lines)))
check(
    "preset 队列保持原顺序",
    [preset_label(p) for p in list_presets(meta_lines)] == ["standard", "renamed-one", "legacy", "empty"],
    str([preset_label(p) for p in list_presets(meta_lines)]),
)
check(
    "人设正文一个字没动",
    read_persona(meta_lines, Target(meta_file, personas_dir, preset_id="renamed-one")) == "带末尾换行\n第二行。",
)
diffs = [(a, b) for a, b in zip(meta_original.decode("utf-8").splitlines(), meta_file.read_text(encoding="utf-8").splitlines()) if a != b]
check(
    "只改了 id/选中项两类行",
    bool(diffs) and all(("id" in a or "selectedDefault" in a) and ("id" in b or "selectedDefault" in b) for a, b in diffs),
    str(diffs[:3]),
)
edit_preset(meta_target, preset_id="mine")
check("改回去之后字节还原", meta_file.read_bytes() == meta_original)

# metadata edits on their own copy: description gets inserted for a preset
# that lacks one, and every field is editable
meta2 = work / "meta2.patch.yml"
shutil.copyfile(patch, meta2)
meta2_target = Target(meta2, work / "m2-personas", preset_id="standard")
edited = edit_preset(meta2_target, name="标准版", description="自带的标准 preset", order=5)
meta2_lines = split_text(meta2.read_text(encoding="utf-8"))["lines"]
standard_meta = next(p for p in list_presets(meta2_lines) if p["preset_id"] == "standard")
check("编辑落在选中的 preset 上", edited["new"] == "standard", str(edited["new"]))
check("显示名已更新", standard_meta["name"] == "标准版", str(standard_meta["name"]))
check("说明被插入", standard_meta["description"] == "自带的标准 preset", str(standard_meta["description"]))
check("排序已更新", str(standard_meta["order"]) == "5", str(standard_meta["order"]))
edit_preset(meta2_target, description="")
meta2_lines = split_text(meta2.read_text(encoding="utf-8"))["lines"]
check("空说明会删掉这一行", next(p for p in list_presets(meta2_lines) if p["preset_id"] == "standard")["description"] is None)
check("其他人设不受影响", [preset_label(p) for p in list_presets(meta2_lines)] == ["standard", "mine", "legacy", "empty"])

# refusals
for kwargs, expect in (
    ({"preset_id": "legacy"}, "已经被"),
    ({"preset_id": "Bad_ID"}, "小写字母"),
    ({"preset_id": "mine"}, "没有需要修改"),
    ({"order": "abc"}, "整数"),
):
    try:
        edit_preset(meta_target, **kwargs)
        check(f"拒绝非法修改 {kwargs}", False, "没有抛错")
    except ValueError as error:
        check(f"拒绝非法修改 {kwargs}", expect in str(error), str(error)[:50])

# CLI wiring: --rename-preset through main()
cli_file = work / "cli-meta.patch.yml"
shutil.copyfile(patch, cli_file)
code = persona_editor.main(
    ["--patch", str(cli_file), "--personas", str(work / "c-personas"), "--preset", "empty", "--rename-preset", "empty-two", "--preset-name", "空空如也"]
)
check("CLI 改名退出码为 0", code == 0)
cli_lines = split_text(cli_file.read_text(encoding="utf-8"))["lines"]
check("CLI 改名生效", any(p["preset_id"] == "empty-two" and p["name"] == "空空如也" for p in list_presets(cli_lines)))

print(f"\nworkdir: {work}")
print("ALL CHECKS PASSED" if failures == 0 else f"{failures} CHECK(S) FAILED")
raise SystemExit(0 if failures == 0 else 1)
