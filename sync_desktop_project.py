"""Register Phone Codex projects in the desktop sidebar while desktop is closed."""
import argparse
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import time


def project_rows(database, root):
    if root is None:
        return database.execute(
            "SELECT id, name, created_at_ms, updated_at_ms FROM projects "
            "WHERE json_valid(metadata) AND json_extract(metadata, '$.phoneCodex') = 'true'"
        ).fetchall()
    return database.execute(
        "SELECT p.id, p.name, p.created_at_ms, p.updated_at_ms FROM projects p "
        "JOIN project_roots r ON p.id=r.project_id WHERE r.path = ? COLLATE NOCASE", (str(root),)
    ).fetchall()


def merge_projects(database, state, home, requested):
    projects = state["local-projects"]
    aliases = state["app-server-project-id-by-legacy-project-id-by-host"].setdefault("local:" + str(home), {})
    assignments = state["thread-project-assignments"]
    projectless = set(state["projectless-thread-ids"])
    changed = []
    for project_id, name, created, updated in {row[0]: row for row in requested}.values():
        roots = [row[0] for row in database.execute(
            "SELECT path FROM project_roots WHERE project_id=? ORDER BY position", (project_id,))]
        if not roots:
            continue
        similar = [key for key, project in projects.items()
                   if aliases.get(key) == project_id
                   or tuple(map(Path, project["rootPaths"])) == tuple(map(Path, roots))]
        if len(similar) > 1:
            continue
        desktop_id = similar[0] if similar else project_id
        newly_registered = desktop_id not in projects
        if desktop_id in aliases and aliases[desktop_id] != project_id:
            # Desktop already created its own project for this folder; keep its identity.
            native_id = aliases[desktop_id]
        else:
            native_id = project_id
        projects.setdefault(desktop_id, {"id": desktop_id, "name": name, "rootPaths": roots,
                                        "createdAt": created, "updatedAt": updated})
        aliases[desktop_id] = native_id
        if desktop_id not in state["project-order"]:
            state["project-order"].append(desktop_id)
        threads = [row[0] for row in database.execute(
            "SELECT id FROM threads WHERE project_id=? AND attached_thread_parent_id IS NULL", (project_id,))]
        added = 0
        for thread in threads:
            if thread in assignments or thread in projectless:
                continue
            assignments[thread] = {"projectKind": "local", "projectId": desktop_id}
            state["thread-project-membership-host-ids"][thread] = "local"
            added += 1
        if newly_registered or added:
            changed.append((name, added))
    return changed


def sync(roots=(), automatic=False, dry_run=False, home=None):
    home = home or Path.home() / ".codex"
    registry = home / ".codex-global-state.json"
    original = registry.read_bytes()
    state = json.loads(original)
    with closing(sqlite3.connect((home / "state_5.sqlite").as_uri() + "?mode=ro", uri=True)) as database:
        rows = project_rows(database, None) if automatic else []
        for root in roots:
            matches = project_rows(database, root)
            if len(matches) != 1:
                if automatic:
                    continue
                raise ValueError(f"应有一个绑定到 {root} 的 Codex 项目，找到 {len(matches)} 个")
            rows.extend(matches)
        changes = merge_projects(database, state, home, rows)
    if dry_run or state == json.loads(original):
        return changes
    if desktop_running():
        return None
    updated = json.dumps(state, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    with tempfile.NamedTemporaryFile(dir=home, suffix=".tmp", delete=False) as output:
        temporary = Path(output.name)
        output.write(updated)
    try:
        if registry.read_bytes() != original or desktop_running():
            return None
        backup = registry.with_name(f"phonecodex-desktop-project-{time.time_ns()}.bak")
        backup.write_bytes(original)
        os.replace(temporary, registry)
    finally:
        temporary.unlink(missing_ok=True)
    return changes


def desktop_running():
    command = (
        "Get-CimInstance Win32_Process -Filter \"Name='Codex.exe' OR Name='ChatGPT.exe'\" -ErrorAction Stop "
        r"| Where-Object { -not $_.ExecutablePath -or $_.ExecutablePath -match '\\app\\(?:Codex|ChatGPT)\.exe$' } "
        "| Select-Object -ExpandProperty ProcessId"
    )
    result = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
                            check=True, capture_output=True, text=True, timeout=10,
                            creationflags=subprocess.CREATE_NO_WINDOW)
    return bool(result.stdout.strip())


def watch(stop, roots):
    last_error = None
    while not stop.is_set():
        try:
            for name, count in sync(roots, automatic=True) or ():
                print(f"桌面项目已登记：{name}（{count} 条会话）", flush=True)
            last_error = None
        except (OSError, ValueError, KeyError, sqlite3.Error, subprocess.SubprocessError) as error:
            if str(error) != last_error:
                print(f"桌面项目登记暂未完成：{error}", flush=True)
                last_error = str(error)
        stop.wait(3)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("folder", type=Path)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    try:
        changes = sync([args.folder.resolve(strict=True)], dry_run=args.dry_run)
        if changes is None:
            raise ValueError("请先完全退出桌面 Codex，再运行同步")
        for name, count in changes:
            print(f"{'可登记' if args.dry_run else '已登记'}项目「{name}」和 {count} 条会话。")
        if not changes:
            print("该项目无需登记。")
    except (OSError, ValueError, KeyError, sqlite3.Error, subprocess.SubprocessError) as error:
        parser.exit(1, f"{error}\n")
