import json
import os
from pathlib import Path
import queue
import shutil
import subprocess
import threading
import uuid
from collections import deque


def find_codex(executable):
    found = shutil.which(executable)
    if found:
        return found
    if executable != "codex":
        return executable
    command = "Get-AppxPackage -Name OpenAI.Codex | Select-Object -ExpandProperty InstallLocation"
    try:
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
             "[Console]::OutputEncoding = [Text.Encoding]::UTF8; " + command],
            check=True, capture_output=True, encoding="utf-8", timeout=8,
        )
        for location in result.stdout.splitlines():
            candidate = Path(location.strip()) / "app" / "resources" / "codex.exe"
            if candidate.is_file():
                return str(candidate)
    except (OSError, subprocess.SubprocessError):
        pass
    raise RuntimeError("找不到 Codex。请使用 --codex 指定电脑版 codex.exe 的完整路径。")


def saved_projects():
    profile_home = Path.home() / ".codex"
    homes = [profile_home]
    if custom_home := os.environ.get("CODEX_HOME"):
        homes.append(Path(custom_home))
    projects = []
    for home in dict.fromkeys(homes):
        try:
            state = json.loads((home / ".codex-global-state.json").read_text(encoding="utf-8-sig"))
            legacy = state.get("local-projects", {})
        except (OSError, ValueError, AttributeError):
            continue
        if not isinstance(legacy, dict):
            continue
        for project_id, entry in legacy.items():
            if not isinstance(entry, dict) or not isinstance(entry.get("rootPaths"), list):
                continue
            paths = [path for path in entry["rootPaths"] if isinstance(path, str) and Path(path).is_absolute()]
            if paths:
                projects.append({"id": project_id, "name": entry.get("name") or Path(paths[0]).name,
                                 "paths": paths})
    return projects


def merge_projects(first, second):
    def path_key(path):
        return os.path.normcase(os.path.normpath(path))

    projects, known_paths, known_ids = [], set(), set()
    for project in [*first, *second]:
        paths = {path_key(path) for path in project["paths"]}
        if project["id"] not in known_ids and (not paths or not paths.issubset(known_paths)):
            projects.append(project)
            known_ids.add(project["id"])
            known_paths.update(paths)
    return projects


class Codex:
    def __init__(self, root, executable, all_drives=False):
        self.root = str(root)
        self.all_drives = all_drives
        self.lock = threading.RLock()
        self.pending = {}
        self.approvals = {}
        self.loaded = set()
        self.events = deque(maxlen=4000)
        self.sequence = 0
        self.generation = uuid.uuid4().hex
        self.process = subprocess.Popen(
            [find_codex(executable), "app-server", "--listen", "stdio://"],
            cwd=root, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            encoding="utf-8", errors="replace", bufsize=1,
        )
        threading.Thread(target=self.read, daemon=True).start()
        try:
            self.call("initialize", {
                "clientInfo": {"name": "phone_codex", "title": "Phone Codex", "version": "1.0.0"},
                "capabilities": {"experimentalApi": True, "requestAttestation": False},
            })
            self.write({"method": "initialized"})
        except Exception:
            self.process.terminate()
            raise

    def write(self, message):
        with self.lock:
            self.process.stdin.write(json.dumps(message, ensure_ascii=False) + "\n")
            self.process.stdin.flush()

    def call(self, method, params):
        request_id = uuid.uuid4().hex
        result = queue.Queue()
        with self.lock:
            self.pending[request_id] = result
        try:
            self.write({"id": request_id, "method": method, "params": params})
            try:
                response = result.get(timeout=30)
            except queue.Empty:
                raise RuntimeError("Codex 响应超时") from None
            if "error" in response:
                raise RuntimeError(response["error"]["message"])
            return response["result"]
        finally:
            with self.lock:
                self.pending.pop(request_id, None)

    def read(self):
        approval_methods = {"item/commandExecution/requestApproval", "item/fileChange/requestApproval"}
        event_methods = {"item/started", "item/completed", "item/agentMessage/delta",
                         "turn/started", "turn/completed", "error"}
        for line in self.process.stdout:
            try:
                message = json.loads(line)
            except ValueError:
                continue
            with self.lock:
                method = message.get("method")
                request_id = message.get("id")
                if method is None:
                    if request_id in self.pending:
                        self.pending[request_id].put(message)
                elif request_id is not None:
                    if method in approval_methods:
                        self.approvals[str(request_id)] = message
                    else:
                        self.write({"id": request_id, "error": {
                            "code": -32601, "message": "This client does not support this request"}})
                elif method == "serverRequest/resolved":
                    self.approvals.pop(str(message["params"]["requestId"]), None)
                elif method in event_methods:
                    self.sequence += 1
                    self.events.append((self.sequence, message))
        with self.lock:
            for result in self.pending.values():
                result.put({"error": {"message": "Codex 服务已退出"}})

    def poll(self, after, thread_id, generation):
        with self.lock:
            if generation != self.generation:
                after = 0
            return {
                "sequence": self.sequence, "generation": self.generation,
                "events": [event for seq, event in self.events if seq > after
                           and event.get("params", {}).get("threadId") == thread_id],
                "approvals": [event for event in self.approvals.values()
                              if event["params"].get("threadId") == thread_id],
            }

    def projects(self):
        projects = []
        cursor = None
        try:
            while True:
                page = self.call("project/list", {"limit": 100, "cursor": cursor})
                projects.extend({"id": project["id"], "name": project["name"], "native": True,
                                 "paths": [root["path"] for root in project["roots"]]}
                                for project in page["data"])
                cursor = page.get("nextCursor")
                if not cursor:
                    break
        except (OSError, RuntimeError):
            pass
        return projects

    def create_project(self, name, folder):
        path = str(folder)
        project = self.call("project/create", {
            "name": name, "roots": [{"path": path}], "metadata": {"phoneCodex": "true"},
            "idempotencyKey": str(uuid.uuid5(uuid.NAMESPACE_URL, name + "\n" + path)),
        })["project"]
        return {"id": project["id"], "name": project["name"], "native": True,
                "paths": [root["path"] for root in project["roots"]]}

    def models(self):
        models, cursor = [], None
        while True:
            page = self.call("model/list", {"limit": 100, "cursor": cursor})
            models.extend({"id": item["model"], "name": item["displayName"], "default": item["isDefault"],
                           "efforts": [option["reasoningEffort"] for option in item["supportedReasoningEfforts"]],
                           "defaultEffort": item["defaultReasoningEffort"]}
                          for item in page["data"] if not item.get("hidden"))
            cursor = page.get("nextCursor")
            if not cursor:
                return models

    def send(self, thread_id, prompt, folder=None, project_id=None, model=None, effort=None, reviewer="user"):
        settings = {"cwd": str(folder or self.root), "sandbox": "workspace-write",
                    "approvalPolicy": "on-request", "approvalsReviewer": reviewer}
        if not thread_id:
            if model:
                settings["model"] = model
            if project_id:
                settings["projectId"] = project_id
            thread_id = self.call("thread/start", settings)["thread"]["id"]
            self.loaded.add(thread_id)
        elif thread_id not in self.loaded:
            previous = self.call("thread/read", {"threadId": thread_id})["thread"]
            project = Path(previous["cwd"]).resolve()
            if not project.is_dir():
                raise ValueError("此会话的项目文件夹已不存在或当前账户无法访问")
            if not self.all_drives and not project.is_relative_to(Path(self.root)):
                raise ValueError("此会话的项目不在共享目录内。请用包含该项目的目录启动服务。")
            settings["cwd"] = str(project)
            self.call("thread/resume", {"threadId": thread_id, "excludeTurns": True, **settings})
            self.loaded.add(thread_id)
        params = {"threadId": thread_id, "approvalsReviewer": reviewer,
                  "input": [{"type": "text", "text": prompt, "text_elements": []}]}
        if model:
            params["model"] = model
        if effort:
            params["effort"] = effort
        turn = self.call("turn/start", params)["turn"]
        return {"threadId": thread_id, "turn": turn}

    def decide(self, request_id, decision):
        with self.lock:
            request = self.approvals.get(str(request_id))
            if request is None:
                raise ValueError("此审批已结束")
            self.write({"id": request["id"], "result": {"decision": decision}})
            del self.approvals[str(request_id)]
