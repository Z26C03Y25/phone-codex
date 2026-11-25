import argparse
import ctypes
import getpass
import hashlib
import ipaddress
import json
import mimetypes
import os
from pathlib import Path, PureWindowsPath
import secrets
import shutil
import ssl
import stat
import subprocess
import tempfile
import threading
import time
from collections import deque
from ctypes import wintypes
from http.cookies import SimpleCookie, CookieError
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, urlsplit

from codex_bridge import Codex, merge_projects, saved_projects
from sync_desktop_project import watch as watch_desktop_projects

HERE = Path(__file__).resolve().parent
MAX_UPLOAD = 512 * 1024 * 1024
SESSION_SECONDS = 8 * 3600
TRASH_NAME = ".phonecodex-trash"
ASSETS = {"/": "index.html", "/ui.css": "ui.css", "/ui.js": "ui.js", "/icon.svg": "icon.svg"}


def verify_password(account, password):
    username, domain = account, "."
    if "\\" in account:
        domain, username = account.split("\\", 1)
    elif "@" in account:
        domain = None
    security = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    logon = security.LogonUserW
    logon.argtypes = (wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.LPCWSTR,
                      wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE))
    logon.restype = wintypes.BOOL
    token = wintypes.HANDLE()
    if not logon(username, domain, password, 2, 0, ctypes.byref(token)):
        return False
    close = kernel.CloseHandle
    close.argtypes = (wintypes.HANDLE,)
    current_process = kernel.GetCurrentProcess
    current_process.restype = wintypes.HANDLE
    open_token = security.OpenProcessToken
    open_token.argtypes = (wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE))
    open_token.restype = wintypes.BOOL
    process_token = wintypes.HANDLE()
    try:
        if not open_token(current_process(), 0x0008, ctypes.byref(process_token)):  # TOKEN_QUERY
            return False
        return same_token_user(security, token, process_token)
    finally:
        if process_token:
            close(process_token)
        close(token)


def same_token_user(security, first, second):
    info = security.GetTokenInformation
    info.argtypes = (wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
                     wintypes.DWORD, ctypes.POINTER(wintypes.DWORD))
    info.restype = wintypes.BOOL
    buffers = []
    for token in (first, second):
        size = wintypes.DWORD()
        info(token, 1, None, 0, ctypes.byref(size))  # TokenUser
        if not size.value:
            return False
        buffer = ctypes.create_string_buffer(size.value)
        if not info(token, 1, buffer, size.value, ctypes.byref(size)):
            return False
        buffers.append(buffer)
    # TOKEN_USER begins with SID_AND_ATTRIBUTES, whose first member is a SID pointer.
    equal = security.EqualSid
    equal.argtypes = (ctypes.c_void_p, ctypes.c_void_p)
    equal.restype = wintypes.BOOL
    return bool(equal(*(ctypes.c_void_p.from_buffer(buffer) for buffer in buffers)))


def filename(name):
    if (not isinstance(name, str) or not name or name in {".", ".."}
            or name.endswith((" ", ".")) or any(c in '<>:"/\\|?*' or ord(c) < 32 for c in name)
            or PureWindowsPath(name).is_reserved() or name.casefold() == TRASH_NAME):
        raise ValueError("文件或文件夹名称无效")
    return name


class Files:
    def __init__(self, root):
        self.root = root.resolve(strict=True) if root is not None else None
        if self.root is not None and not self.root.is_dir():
            raise ValueError("共享路径必须是文件夹")
        self.lock = threading.RLock()

    @property
    def label(self):
        return str(self.root) if self.root is not None else "此电脑"

    def drives(self):
        mask = ctypes.WinDLL("kernel32").GetLogicalDrives()
        return [chr(ord("A") + index) + ":" for index in range(26) if mask & (1 << index)]

    def resolve(self, relative):
        if not isinstance(relative, str):
            raise ValueError("文件路径无效")
        parts = relative.split("/") if relative else []
        base = self.root
        if base is None:
            if not parts:
                raise ValueError("请先打开一个磁盘")
            drive = parts.pop(0).upper()
            if drive not in self.drives():
                raise ValueError("磁盘不存在或尚未连接")
            base = Path(drive + "\\")
        current = base
        for part in parts:
            current /= filename(part)
            try:
                info = current.lstat()
            except FileNotFoundError:
                continue
            if getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT or stat.S_ISLNK(info.st_mode):
                raise ValueError("无法访问链接或重解析点")
            if self.root is not None and stat.S_ISREG(info.st_mode) and info.st_nlink > 1:
                raise ValueError("限定目录模式下无法访问硬链接文件")
            current = current.resolve(strict=True)
            if not current.is_relative_to(base) or any(
                part.casefold() == TRASH_NAME for part in current.relative_to(base).parts
            ):
                raise ValueError("无法访问此路径")
        return current

    def trash(self, directory):
        trash = (self.root if self.root is not None else directory) / TRASH_NAME
        trash.mkdir(exist_ok=True)
        info = trash.lstat()
        if not trash.is_dir() or trash.is_symlink() or getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
            raise ValueError("回收目录不可用")
        return trash

    def project_folder(self, absolute):
        if not isinstance(absolute, str) or not Path(absolute).is_absolute():
            raise ValueError("项目路径无效")
        path = Path(absolute)
        if self.root is not None:
            if not path.is_relative_to(self.root):
                raise ValueError("项目位于共享目录外，请使用全盘模式")
            parts = path.relative_to(self.root).parts
        else:
            parts = (path.drive, *path.parts[1:])
        relative = "/".join(parts)
        if not self.resolve(relative).is_dir():
            raise ValueError("项目文件夹不存在或无法访问")
        return relative

    def listing(self, relative):
        if self.root is None and relative == "":
            return {"path": "", "items": [{"name": drive, "folder": True, "drive": True}
                                          for drive in self.drives()]}
        directory = self.resolve(relative)
        rows = []
        for child in directory.iterdir():
            if child.name.casefold() == TRASH_NAME:
                continue
            try:
                info = child.lstat()
            except FileNotFoundError:
                continue
            if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
                continue
            rows.append({"name": child.name, "folder": stat.S_ISDIR(info.st_mode),
                         "size": info.st_size, "modified": info.st_mtime})
        rows.sort(key=lambda row: (not row["folder"], row["name"].casefold()))
        return {"path": relative, "items": rows}

    def change(self, action, data):
        relative = data.get("path", "")
        with self.lock:
            target = self.resolve(relative)
            if action == "mkdir":
                (target / filename(data.get("name"))).mkdir()
            elif not relative or target == target.parent:
                raise ValueError("无法修改共享根目录")
            elif action == "rename":
                new = target.with_name(filename(data.get("name")))
                if new.exists() and str(new).casefold() != str(target).casefold():
                    raise FileExistsError
                target.rename(new)
            elif action == "delete":
                slot = Path(tempfile.mkdtemp(dir=self.trash(target.parent)))
                try:
                    target.rename(slot / target.name)
                except OSError:
                    slot.rmdir()
                    raise


class BridgeServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, files, account, codex_executable, lan):
        super().__init__(address, Handler)
        port_suffix = "" if self.server_port == 443 else f":{self.server_port}"
        self.origin = f"https://{address[0]}{port_suffix}"
        self.files = files
        self.lan = lan
        self.account = account
        self.sessions = {}
        self.connections = threading.BoundedSemaphore(32)
        self.attempts = deque()
        self.auth_lock = threading.Lock()
        self.codex_lock = threading.Lock()
        self.turn_lock = threading.Lock()
        self.codex = None
        self.codex_executable = codex_executable

    def bridge(self):
        with self.codex_lock:
            if self.codex is None or self.codex.process.poll() is not None:
                self.codex = Codex(self.files.root or Path.cwd(), self.codex_executable,
                                   all_drives=self.files.root is None)
            return self.codex

    def get_request(self):
        connection, address = self.socket.accept()
        connection.settimeout(30)
        return connection, address

    def process_request_thread(self, request, client_address):
        try:
            try:
                request = self.tls.wrap_socket(request, server_side=True)
            except OSError:
                request.close()
                return
            super().process_request_thread(request, client_address)
        finally:
            self.connections.release()

    def process_request(self, request, client_address):
        if ipaddress.IPv4Address(client_address[0]) not in self.lan or not self.connections.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self.connections.release()
            raise


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def reply(self, status, content, mime, headers=None):
        self.send_response(status)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy",
                         "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(content)

    def json(self, status, data, headers=None):
        self.reply(status, json.dumps(data, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8", headers)

    def body_size(self, maximum):
        size = int(self.headers.get("Content-Length", "-1"))
        if self.headers.get("Transfer-Encoding") or not 0 <= size <= maximum:
            raise ValueError("请求过大或长度无效")
        return size

    def input(self):
        if self.headers.get_content_type() != "application/json":
            raise ValueError("请求格式无效")
        value = json.loads(self.rfile.read(self.body_size(128 * 1024)))
        if not isinstance(value, dict):
            raise ValueError("请求格式无效")
        return value

    def session(self):
        try:
            cookie = SimpleCookie(self.headers.get("Cookie", ""))
        except CookieError:
            return None
        token = cookie.get("phonecodex")
        with self.server.auth_lock:
            if token and self.server.sessions.get(token.value, 0) > time.monotonic():
                return token.value
        return None

    def login(self):
        password = self.input().get("password")
        if not isinstance(password, str) or not 0 < len(password) <= 512 or "\0" in password:
            raise ValueError("请输入 Windows 账户密码")
        with self.server.auth_lock:
            now = time.monotonic()
            attempts = self.server.attempts
            while attempts and attempts[0] < now - 300:
                attempts.popleft()
            if len(attempts) >= 5:
                return self.json(429, {"error": "尝试次数过多，请 5 分钟后再试"})
            if not verify_password(self.server.account, password):
                attempts.append(now)
                return self.json(401, {"error": "Windows 密码验证失败"})
            attempts.clear()
            self.server.sessions = {key: expires for key, expires in self.server.sessions.items() if expires > now}
            token = secrets.token_urlsafe(32)
            self.server.sessions[token] = now + SESSION_SECONDS
        self.json(200, {"ok": True}, {"Set-Cookie":
                  f"phonecodex={token}; Path=/; Secure; HttpOnly; SameSite=Strict; Max-Age={SESSION_SECONDS}"})

    def upload(self, folder, name):
        size = self.body_size(MAX_UPLOAD)
        files = self.server.files
        with files.lock:
            directory = files.resolve(folder)
            target = directory / filename(name)
            if not directory.is_dir():
                raise FileNotFoundError
            if target.exists():
                raise FileExistsError
            original_directory = directory.stat()
            temporary = tempfile.NamedTemporaryFile(dir=files.trash(directory), delete=False)
        try:
            with temporary:
                remaining = size
                while remaining:
                    block = self.rfile.read(min(1024 * 1024, remaining))
                    if not block:
                        raise ValueError("上传中断")
                    temporary.write(block)
                    remaining -= len(block)
            with files.lock:
                directory = files.resolve(folder)
                if not os.path.samestat(original_directory, directory.stat()):
                    raise ValueError("上传期间目标文件夹发生变化，请重试")
                target = directory / filename(name)
                os.rename(temporary.name, target)
        finally:
            Path(temporary.name).unlink(missing_ok=True)
        self.json(200, {"ok": True})

    def download(self, relative):
        files = self.server.files
        with files.lock:
            target = files.resolve(relative)
            if not target.is_file():
                raise FileNotFoundError
            source = target.open("rb")
        with source:
            info = os.fstat(source.fileno())
            if files.root is not None and info.st_nlink > 1:
                raise ValueError("限定目录模式下无法访问硬链接文件")
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Disposition", "attachment; filename*=UTF-8''" + quote(target.name, safe=""))
            self.send_header("Content-Length", str(info.st_size))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            shutil.copyfileobj(source, self.wfile, 1024 * 1024)

    def route(self, post):
        url = urlsplit(self.path)
        path = url.path
        query = {key: values[0] for key, values in parse_qs(url.query, keep_blank_values=True).items()}
        if self.headers.get("Host") != urlsplit(self.server.origin).netloc:
            return self.json(403, {"error": "请使用启动时显示的地址"})
        if post and self.headers.get("Origin") != self.server.origin:
            return self.json(403, {"error": "请求来源无效"})
        if not post and path in ASSETS:
            asset = HERE / "web" / ASSETS[path]
            mime = mimetypes.guess_type(asset.name)[0]
            if mime.startswith("text/"):
                mime += "; charset=utf-8"
            return self.reply(200, asset.read_bytes(), mime)
        if post and path == "/api/login":
            return self.login()
        token = self.session()
        if not token:
            return self.json(401, {"error": "请登录"})
        relative = query.get("path", "")
        files = self.server.files
        if not post and path == "/api/session":
            return self.json(200, {"root": files.label, "allDrives": files.root is None,
                                   "defaultProject": str(files.root or Path.cwd())})
        if post and path == "/api/logout":
            with self.server.auth_lock:
                self.server.sessions.pop(token, None)
            return self.json(200, {}, {"Set-Cookie": "phonecodex=; Path=/; Secure; HttpOnly; SameSite=Strict; Max-Age=0"})
        if not post and path == "/api/files":
            with files.lock:
                result = files.listing(relative)
            return self.json(200, result)
        if not post and path == "/api/project-folder":
            with files.lock:
                return self.json(200, {"path": files.project_folder(relative)})
        if not post and path == "/api/download":
            return self.download(relative)
        if post and path == "/api/upload":
            return self.upload(relative, query.get("name"))
        if post and path in {"/api/mkdir", "/api/rename", "/api/delete"}:
            files.change(path.rsplit("/", 1)[-1], self.input())
            return self.json(200, {"ok": True})
        if path.startswith("/api/codex/"):
            return self.codex(path.rsplit("/", 1)[-1], query, post)
        self.json(404, {"error": "找不到此页面"})

    def codex(self, action, query, post):
        if action == "saved-projects" and not post:
            return self.json(200, merge_projects([], saved_projects()))
        bridge = self.server.bridge()
        data = self.input() if post else query
        thread_id = data.get("threadId")
        if action == "threads" and not post:
            result = bridge.call("thread/list", {"limit": 40, "sortKey": "updated_at", "currentAccountOnly": True,
                                               "archived": data.get("archived") == "true",
                                               "cursor": data.get("cursor")})
        elif action == "projects" and not post:
            result = merge_projects(bridge.projects(), saved_projects())
        elif action == "models" and not post:
            result = bridge.models()
        elif action == "projects" and post:
            name = data.get("name")
            if not isinstance(name, str) or not name.strip() or len(name) > 120:
                raise ValueError("请输入 1 到 120 个字符的项目名称")
            with self.server.files.lock:
                folder = self.server.files.resolve(data.get("folder"))
                if not folder.is_dir():
                    raise ValueError("项目目录不存在")
            result = bridge.create_project(name.strip(), folder)
        elif action == "history" and not post and thread_id:
            result = bridge.call("thread/read", {"threadId": thread_id, "includeTurns": True})
        elif action == "events" and not post and thread_id:
            result = bridge.poll(int(data.get("after", 0)), thread_id, data.get("generation"))
        elif action == "send" and post:
            prompt = data.get("text")
            if not isinstance(prompt, str) or not prompt.strip():
                raise ValueError("请输入消息")
            project = None
            if not thread_id and data.get("folder") is not None:
                with self.server.files.lock:
                    project = self.server.files.resolve(data["folder"])
                    if not project.is_dir():
                        raise ValueError("请选择项目文件夹")
            project_id = data.get("projectId")
            model = data.get("model")
            effort = data.get("effort")
            reviewer = data.get("reviewer", "user")
            if reviewer not in ("user", "auto_review"):
                raise ValueError("审批方式无效")
            for value in (project_id, model):
                if value is not None and (not isinstance(value, str) or not 0 < len(value) <= 200):
                    raise ValueError("项目或模型无效")
            if effort is not None and (not isinstance(effort, str) or effort not in {
                "none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"
            }):
                raise ValueError("推理深度无效")
            with self.server.turn_lock:
                result = bridge.send(thread_id, prompt.strip(), project, project_id, model, effort, reviewer)
        elif action == "stop" and post and thread_id and data.get("turnId"):
            result = bridge.call("turn/interrupt", {"threadId": thread_id, "turnId": data["turnId"]})
        elif action == "approval" and post and data.get("decision") in ("accept", "decline"):
            bridge.decide(data.get("id"), data["decision"])
            result = {}
        else:
            return self.json(404, {"error": "未知的 Codex 操作"})
        self.json(200, result)

    def dispatch(self, post):
        try:
            self.route(post)
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            pass
        except FileExistsError:
            self.json(409, {"error": "已存在同名项目"})
        except (FileNotFoundError, NotADirectoryError):
            self.json(404, {"error": "文件夹或文件已不存在"})
        except PermissionError:
            self.json(403, {"error": "Windows 拒绝了此操作，请检查权限或文件占用"})
        except (ValueError, TypeError) as error:
            self.json(400, {"error": str(error)})
        except (OSError, RuntimeError) as error:
            self.json(502, {"error": str(error)})

    def do_GET(self):
        self.dispatch(False)

    def do_POST(self):
        self.dispatch(True)


def tls_context(ip, cert_path, key_path):
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    if cert_path:
        context.load_cert_chain(cert_path, key_path)
        return context, None
    openssl = shutil.which("openssl")
    if not openssl and (git := shutil.which("git")):
        candidate = Path(git).parent.parent / "mingw64" / "bin" / "openssl.exe"
        if candidate.is_file():
            openssl = str(candidate)
    if not openssl:
        raise SystemExit("需要 OpenSSL（Git for Windows 通常自带），也可传入 --cert 和 --key")
    generated = subprocess.run(
        [openssl, "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "7",
         "-subj", "/CN=Phone Codex", "-addext", f"subjectAltName=IP:{ip}",
         "-keyout", "-", "-out", "-"], check=True, capture_output=True,
    ).stdout
    certificate = generated[generated.index(b"-----BEGIN CERTIFICATE-----"):]
    digest = hashlib.sha256(ssl.PEM_cert_to_DER_cert(certificate.decode("ascii"))).digest()
    with tempfile.TemporaryDirectory(prefix="phonecodex-tls-") as directory:
        pem = Path(directory) / "server.pem"
        pem.write_bytes(generated)
        context.load_cert_chain(pem)
    return context, digest.hex(":").upper()


def local_network(ip):
    if ip.is_loopback:
        return ipaddress.IPv4Network(f"{ip}/32")
    command = (
        f"Get-NetIPAddress -AddressFamily IPv4 -IPAddress '{ip}' -ErrorAction Stop "
        "| Select-Object -ExpandProperty PrefixLength -Unique"
    )
    try:
        result = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
                                check=True, capture_output=True, text=True, timeout=15)
        prefix = int(result.stdout.strip())
        lan = ipaddress.IPv4Network((ip, prefix), strict=False)
    except (OSError, ValueError, subprocess.SubprocessError):
        raise ValueError(f"无法确定 {ip} 的本地网段。请用 ipconfig 核对热点网卡的 IPv4 地址。") from None
    if not lan.is_private:
        raise ValueError(f"{lan} 不是私有局域网，服务已拒绝启动。")
    return lan


def main():
    parser = argparse.ArgumentParser(description="在手机热点内访问 Windows 上的 Codex")
    parser.add_argument("--ip", required=True, help="Windows 连接手机热点后获得的 IPv4 地址")
    scope = parser.add_mutually_exclusive_group(required=True)
    scope.add_argument("--root", type=Path, help="允许手机访问的文件夹")
    scope.add_argument("--all-drives", action="store_true", help="开放当前 Windows 账户可访问的盘符")
    parser.add_argument("--sync-desktop-projects", action="store_true", help="桌面 Codex 退出后自动登记手机创建的项目")
    parser.add_argument("--sync-existing-project", type=Path, action="append", default=[],
                        help="同时登记以前创建的指定项目目录，可重复")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--account", default=getpass.getuser(), help="Windows 用户名，默认为当前用户")
    parser.add_argument("--codex", default="codex", help="Codex CLI 路径")
    parser.add_argument("--cert", type=Path, help="可选 TLS PEM 证书")
    parser.add_argument("--key", type=Path, help="可选 TLS PEM 私钥")
    args = parser.parse_args()
    try:
        ip = ipaddress.IPv4Address(args.ip)
    except ValueError:
        parser.error("请输入 Windows 热点网卡的 IPv4 地址")
    if not (ip.is_private or ip.is_loopback) or ip.is_unspecified or ip.is_multicast:
        parser.error("请输入 Windows 的私网 IPv4 地址")
    if bool(args.cert) != bool(args.key):
        parser.error("--cert 和 --key 必须一起提供")
    if os.name != "nt":
        parser.error("此程序需要 Windows")
    try:
        files = Files(args.root)
    except (OSError, ValueError):
        parser.error(f"无法打开共享目录 {args.root}。请确认目录存在，路径有空格时请使用双引号。")
    try:
        lan = local_network(ip)
    except ValueError as error:
        parser.error(str(error))
    tls, fingerprint = tls_context(ip, args.cert, args.key)
    with BridgeServer((str(ip), args.port), files, args.account, args.codex, lan) as server:
        server.tls = tls
        print(f"手机浏览器打开 {server.origin}", flush=True)
        print(f"仅接受来自局域网 {lan} 的连接", flush=True)
        if fingerprint:
            print(f"临时证书 SHA-256 {fingerprint}", flush=True)
        print(f"共享范围 {server.files.label} | Windows 账户 {args.account} | Ctrl+C 停止", flush=True)
        stop_sync = threading.Event()
        if args.sync_desktop_projects or args.sync_existing_project:
            print("桌面项目自动登记已启用，等待桌面 Codex 完全退出", flush=True)
            roots = [path.resolve() for path in args.sync_existing_project]
            threading.Thread(target=watch_desktop_projects, args=(stop_sync, roots), daemon=True).start()
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            stop_sync.set()
            if server.codex:
                server.codex.process.terminate()


if __name__ == "__main__":
    main()
