"use strict";

const $ = id => document.getElementById(id);
const state = {
  threadId: null, title: "", threads: [], cursor: null, items: new Map(),
  finished: new Set(), turnId: null, sequence: 0, generation: "",
  selection: 0, sending: false, loading: false, folder: "", rows: [], fileLoad: 0,
  allDrives: false, root: "", defaultProject: "", chatFolder: null, archived: false, threadLoad: 0, projects: [],
  pickingProject: false, chatProject: null,
  models: [], modelEfforts: new Map(),
};
let toastTimer;
let dialogAction;

function node(tag, text, className) {
  const element = document.createElement(tag);
  if (text !== undefined) element.textContent = text;
  if (className) element.className = className;
  return element;
}

function button(text, action, className) {
  const control = node("button", text, className);
  control.type = "button";
  control.addEventListener("click", action);
  return control;
}

function toast(message) {
  $("toast").textContent = message;
  $("toast").hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => $("toast").hidden = true, 5200);
}

function url(path, params) {
  return path + "?" + new URLSearchParams(params);
}

async function api(path, options) {
  const response = await fetch(path, options);
  const result = await response.json();
  if (!response.ok) {
    if (response.status === 401 && path !== "/api/login") showLogin();
    throw new Error(result.error || "请求失败");
  }
  return result;
}

function post(path, body) {
  return api(path, {
    method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body),
  });
}

function sidebar(open) {
  $("sidebar").classList.toggle("open", open);
  $("scrim").hidden = !open;
  $("open-sidebar").setAttribute("aria-expanded", String(open));
}

function tab(files) {
  $("chat").hidden = files;
  $("files").hidden = !files;
  $("show-chat").setAttribute("aria-pressed", String(!files));
  $("show-files").setAttribute("aria-pressed", String(files));
  $("title").textContent = files ? "共享文件" : state.title;
  sidebar(false);
}

function showLogin() {
  state.selection++;
  state.fileLoad++;
  $("app").hidden = true;
  $("login").hidden = false;
  $("password").value = "";
  $("login-error").textContent = "";
}

async function enter(session) {
  state.folder = "";
  state.threads = [];
  state.cursor = null;
  state.sending = false;
  state.allDrives = session.allDrives;
  state.root = session.root;
  state.defaultProject = session.defaultProject;
  state.archived = false;
  state.projects = [];
  $("archived").setAttribute("aria-pressed", "false");
  $("prompt").value = "";
  $("filter").value = "";
  $("shared-root").textContent = session.allDrives
    ? "全盘模式 · 从盘符进入"
    : "仅共享 " + session.root + " · 要浏览全部盘符，请用 --all-drives 重启服务";
  $("login").hidden = true;
  $("app").hidden = false;
  selectThread(null);
  reloadProjects();
  loadModels().catch(error => {
    $("model").options[0].textContent = "模型列表不可用";
    toast(error.message);
  });
}

async function loadModels() {
  const models = await api("/api/codex/models");
  state.models = models;
  const picker = $("model");
  const previous = picker.value;
  const fallback = models.find(model => model.default);
  picker.replaceChildren(new Option(fallback ? "默认 · " + fallback.name : "默认模型", ""),
    ...models.map(model => new Option(model.name, model.id)));
  if (models.some(model => model.id === previous)) picker.value = previous;
  renderEfforts();
}

function selectedModel() {
  const selected = $("model").value;
  return state.models.find(item => selected ? item.id === selected : item.default);
}

function renderEfforts() {
  const model = selectedModel();
  const picker = $("effort");
  const labels = {none:"无", minimal:"最低", low:"低", medium:"中", high:"高",
    xhigh:"极高", max:"最高", ultra:"超高"};
  const efforts = model?.efforts || [];
  const defaultLabel = labels[model?.defaultEffort];
  picker.replaceChildren(new Option(defaultLabel ? "默认 · " + defaultLabel
    : model ? "推理深度" : "先选模型", ""),
    ...efforts.map(effort => new Option(labels[effort] || effort, effort)));
  const previous = state.modelEfforts.get(model?.id);
  if (efforts.includes(previous)) picker.value = previous;
  picker.disabled = !efforts.length;
}

$("model").onchange = renderEfforts;
$("effort").onchange = () => {
  const model = selectedModel();
  if (model) state.modelEfforts.set(model.id, $("effort").value);
};

async function reloadProjects() {
  const status = $("project-status");
  status.hidden = false;
  status.textContent = "正在读取桌面项目…";
  $("retry-projects").hidden = true;
  let liveProjectsLoaded = false;
  const saved = api("/api/codex/saved-projects").then(projects => {
    if (!liveProjectsLoaded && !$("app").hidden) {
      state.projects = projects;
      renderThreads();
    }
  });
  const live = api("/api/codex/projects").then(projects => {
    liveProjectsLoaded = true;
    state.projects = projects;
    renderThreads();
  });
  const results = await Promise.allSettled([saved, live, loadThreads()]);
  const failures = results.slice(1).filter(result => result.status === "rejected");
  status.hidden = failures.length === 0;
  if (failures.length) {
    status.textContent = "Codex 连接失败 · " + failures[0].reason.message;
    $("retry-projects").hidden = false;
  }
  renderThreads();
}

async function loadThreads(append = false) {
  const request = ++state.threadLoad;
  const params = {archived: state.archived};
  if (append && state.cursor) params.cursor = state.cursor;
  const response = await api(url("/api/codex/threads", params));
  if (request !== state.threadLoad || $("app").hidden) return;
  const threads = append ? [...state.threads, ...response.data] : response.data;
  state.threads = [...new Map(threads.map(thread => [thread.id, thread])).values()];
  state.cursor = response.nextCursor;
  renderThreads();
}

function threadTitle(thread) {
  return thread?.name || thread?.preview || "新建会话";
}

function renderThreads() {
  const projects = new Map();
  const roots = [];
  const pathKey = path => path.replaceAll("/", "\\").replace(/\\+$/, "").toLowerCase();
  function addProject(key, name, paths, nativeId = null) {
    const group = node("details", undefined, "project");
    group.open = true;
    group.append(node("summary", name || "其他会话"));
    for (const path of paths) {
      const link = button(path, () => openProject(path), "project-path project-link");
      link.title = "浏览项目文件：" + path;
      group.append(link);
    }
    for (const path of paths) {
      const suffix = paths.length > 1 ? " · " + path : "";
      group.append(button("＋ 新建会话" + suffix, () => newProjectChat(path, name, nativeId),
        "project-link project-new"));
    }
    projects.set(key, group);
  }
  for (const project of state.projects) {
    addProject(project.id, project.name, project.paths, project.native ? project.id : null);
    roots.push(...project.paths.map(path => ({path: pathKey(path), id: project.id})));
  }
  roots.sort((a, b) => b.path.length - a.path.length);
  for (const thread of state.threads) {
    const path = thread.cwd || "";
    const normalized = pathKey(path);
    const match = roots.find(root => normalized === root.path || normalized.startsWith(root.path + "\\"));
    const key = projects.has(thread.projectId) ? thread.projectId : match?.id || normalized;
    if (!projects.has(key)) {
      const folder = path.replace(/[\\/]+$/, "").split(/[\\/]/).pop();
      addProject(key, folder, path ? [path] : []);
    }
    const control = button(threadTitle(thread), () => selectThread(thread), "thread");
    control.setAttribute("aria-current", String(thread.id === state.threadId));
    projects.get(key).append(control);
  }
  $("threads").replaceChildren(...projects.values());
  if (!projects.size && $("project-status").hidden) $("threads").append(node("p", state.archived ? "暂无归档会话" : "暂无项目会话", "project-path"));
  $("more").hidden = !state.cursor;
}

async function openProject(path) {
  try {
    const result = await api(url("/api/project-folder", {path}));
    tab(true);
    await openFolder(result.path);
  } catch (error) {
    toast(error.message);
  }
}

async function newProjectChat(path, name, id) {
  try {
    const result = await api(url("/api/project-folder", {path}));
    selectThread(null, result.path, {name, id});
  } catch (error) {
    toast(error.message);
  }
}

function showStatus(status) {
  $("stop").hidden = !state.turnId;
  $("send").disabled = state.sending || state.loading || Boolean(state.turnId);
  if (status !== undefined) $("turn-status").textContent = status === "Codex" ? "" : status;
}

function itemText(item) {
  switch (item.type) {
    case "userMessage": return item.content.map(value => value.text || (value.type === "text" ? "" : "[附件]")).join("\n");
    case "agentMessage":
    case "plan": return item.text;
    case "commandExecution": return "命令 · " + item.command;
    case "fileChange": return "文件变更 · " + item.changes.map(change => change.path).join("、");
    default: return "";
  }
}

function inlineMarkdown(parent, text) {
  const token = /\x60([^\x60\n]+)\x60|\*\*([^*\n]+)\*\*|\[([^\]\n]+)\]\((https?:\/\/[^\s)]+)\)|\*([^*\n]+)\*/gi;
  let offset = 0;
  for (const match of text.matchAll(token)) {
    parent.append(text.slice(offset, match.index));
    const [, code, strong, label, address, emphasis] = match;
    if (address) {
      const link = node("a", label);
      link.href = address;
      link.target = "_blank";
      link.rel = "noopener noreferrer";
      parent.append(link);
    } else {
      parent.append(node(code ? "code" : strong ? "strong" : "em", code || strong || emphasis));
    }
    offset = match.index + match[0].length;
  }
  parent.append(text.slice(offset));
}

function messageContent(article, text) {
  const lines = text.split(/\r?\n/);
  let index = 0;
  while (index < lines.length) {
    const line = lines[index++];
    if (!line.trim()) continue;
    const fence = line.match(/^\s*(\x60{3,}|~{3,})\s*(.*?)\s*$/);
    const heading = line.match(/^(#{1,6})\s+(.+)$/);
    const listItem = /^\s*(?:([-+*])|(\d+)[.)])\s+(.+)$/;
    const item = line.match(listItem);
    if (fence) {
      const code = node("div", undefined, "code-block");
      if (fence[2]) code.append(node("span", fence[2], "code-language"));
      const block = [];
      while (index < lines.length && !lines[index].trim().startsWith(fence[1])) block.push(lines[index++]);
      if (index < lines.length) index++;
      const pre = node("pre");
      pre.append(node("code", block.join("\n")));
      code.append(pre);
      article.append(code);
    } else if (heading) {
      const title = node("h" + Math.min(heading[1].length + 1, 6));
      inlineMarkdown(title, heading[2]);
      article.append(title);
    } else if (item) {
      const list = node(item[2] ? "ol" : "ul");
      if (item[2]) list.start = Number(item[2]);
      let entry = item;
      while (entry && Boolean(entry[2]) === Boolean(item[2])) {
        const li = node("li");
        inlineMarkdown(li, entry[3]);
        list.append(li);
        entry = lines[index]?.match(listItem);
        if (entry && Boolean(entry[2]) === Boolean(item[2])) index++;
      }
      article.append(list);
    } else {
      const paragraph = node("p");
      const prose = [line];
      while (index < lines.length && lines[index].trim()
          && !/^\s*(?:\x60{3,}|~{3,}|#{1,6}\s)/.test(lines[index])
          && !listItem.test(lines[index])) prose.push(lines[index++]);
      inlineMarkdown(paragraph, prose.join("\n"));
      article.append(paragraph);
    }
  }
}

function renderChat(forceScroll = false) {
  const scroller = $("scroll");
  const atBottom = scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight < 110;
  const messages = $("messages");
  const openActivity = new Set([...messages.querySelectorAll("details[open]")].map(group => group.dataset.first));
  const fragment = document.createDocumentFragment();
  let activity = null;
  let activityCount = 0;
  for (const item of state.items.values()) {
    const text = itemText(item);
    if (!text) continue;
    if (item.type === "commandExecution" || item.type === "fileChange") {
      if (!activity) {
        activity = node("details", undefined, "activity-group");
        activity.dataset.first = item.id;
        activity.open = openActivity.has(item.id);
        activity.append(node("summary"));
        fragment.append(activity);
        activityCount = 0;
      }
      activity.firstChild.textContent = "操作记录 · " + (++activityCount) + " 项";
      activity.append(node("pre", text));
      continue;
    }
    activity = null;
    const article = node("article", undefined, "message " + item.type);
    if (item.type === "userMessage") article.textContent = text;
    else messageContent(article, text);
    fragment.append(article);
  }
  messages.replaceChildren(fragment);
  $("welcome").hidden = Boolean(state.threadId) || state.items.size > 0;
  if (atBottom || forceScroll) scroller.scrollTop = scroller.scrollHeight;
}

async function selectThread(thread, folder = null, project = null) {
  if (state.sending) return;
  const selection = ++state.selection;
  state.threadId = thread?.id || null;
  state.title = project ? project.name + " · 新会话" : threadTitle(thread);
  state.chatFolder = folder;
  state.chatProject = project;
  const partial = folder?.replaceAll("/", "\\");
  $("chat-root").textContent = !folder ? state.defaultProject
    : state.allDrives ? partial + (partial.endsWith(":") ? "\\" : "")
    : state.root.replace(/[\\/]$/, "") + "\\" + partial;
  state.turnId = null;
  state.sequence = 0;
  state.generation = "";
  state.finished.clear();
  state.items.clear();
  state.loading = Boolean(thread);
  $("approvals").replaceChildren();
  tab(false);
  renderThreads();
  renderChat(true);
  showStatus(thread ? "正在读取会话…" : "Codex");
  if (!thread) return;
  try {
    const {thread: history} = await api(url("/api/codex/history", {threadId: thread.id}));
    if (state.selection !== selection) return;
    for (const turn of history.turns) {
      for (const item of turn.items) {
        state.items.set(item.id, item);
        if (turn.status !== "inProgress") state.finished.add(item.id);
      }
      if (turn.status === "inProgress") state.turnId = turn.id;
    }
    renderChat(true);
    showStatus(state.turnId ? "正在处理…" : "Codex");
  } catch (error) {
    if (state.selection === selection) {
      showStatus("会话读取失败");
      toast(error.message);
    }
  } finally {
    if (state.selection === selection) {
      state.loading = false;
      showStatus();
    }
  }
}

function applyEvent(event) {
  const p = event.params;
  switch (event.method) {
    case "turn/started":
      state.turnId = p.turn.id;
      showStatus("正在处理…");
      break;
    case "turn/completed":
      if (p.turn.id === state.turnId) {
        state.turnId = null;
        const labels = {failed: "任务失败", interrupted: "已停止", completed: "已完成"};
        showStatus(labels[p.turn.status] || "Codex");
        if (p.turn.error?.message) toast(p.turn.error.message);
        loadThreads().catch(() => {});
      }
      break;
    case "item/started":
      if (!state.finished.has(p.item.id)) state.items.set(p.item.id, p.item);
      break;
    case "item/completed":
      state.items.set(p.item.id, p.item);
      state.finished.add(p.item.id);
      break;
    case "item/agentMessage/delta":
      if (!state.finished.has(p.itemId)) {
        const item = state.items.get(p.itemId) || {id: p.itemId, type: "agentMessage", text: ""};
        item.text += p.delta;
        state.items.set(p.itemId, item);
      }
      break;
  }
}

async function approval(id, decision) {
  try {
    await post("/api/codex/approval", {id, decision});
  } catch (error) {
    toast(error.message);
  }
}

function renderApprovals(approvals) {
  const container = $("approvals");
  const key = JSON.stringify(approvals.map(request => request.id));
  if (container.dataset.key === key) return;
  container.dataset.key = key;
  container.replaceChildren(...approvals.map(request => {
    const card = node("div", undefined, "approval");
    card.append(node("strong", request.params.command ? "允许执行此命令？" : "允许修改文件？"),
      node("pre", [request.params.command, request.params.cwd, request.params.reason,
        request.params.grantRoot, request.params.networkApprovalContext?.host].filter(Boolean).join("\n")),
      button("拒绝", () => approval(request.id, "decline")),
      button("允许一次", () => approval(request.id, "accept"), "primary"));
    return card;
  }));
}

let lastPollError = false;
async function poll() {
  try {
    if ($("app").hidden || !state.threadId || state.loading || state.sending) return;
    const selection = state.selection;
    const response = await api(url("/api/codex/events", {
      threadId: state.threadId, after: state.sequence, generation: state.generation,
    }));
    if (selection !== state.selection || state.sending) return;
    if (state.generation && state.generation !== response.generation) {
      state.turnId = null;
      showStatus("Codex 已重新连接");
    }
    state.generation = response.generation;
    state.sequence = response.sequence;
    for (const event of response.events) applyEvent(event);
    if (response.events.length) renderChat();
    renderApprovals(response.approvals);
    lastPollError = false;
  } catch (error) {
    if (!lastPollError && !$("app").hidden) toast(error.message);
    lastPollError = true;
  } finally {
    setTimeout(poll, 750);
  }
}

function dialog({title, note = "", name, label = "确定", action}) {
  $("dialog-title").textContent = title;
  $("dialog-note").textContent = note;
  $("dialog-note").hidden = !note;
  $("dialog-name").hidden = name === undefined;
  $("dialog-name").required = name !== undefined;
  $("dialog-name").value = name || "";
  $("dialog-confirm").textContent = label;
  dialogAction = action;
  $("dialog").showModal();
}

function fileUrl(path) {
  return url("/api/download", {path});
}

async function prepareShare(path, name, size) {
  if (!navigator.share || !navigator.canShare || size > 64 * 1024 * 1024) {
    toast("请先下载，再从手机的文件管理器分享给微信");
    return;
  }
  try {
    toast("正在准备文件…");
    const response = await fetch(fileUrl(path));
    if (!response.ok) throw new Error("无法读取文件，请刷新后重试");
    const blob = await response.blob();
    const ext = name.split(".").pop().toLowerCase();
    const mimes = {png: "image/png", jpg: "image/jpeg", jpeg: "image/jpeg",
      pdf: "application/pdf", txt: "text/plain", mp4: "video/mp4"};
    const file = new File([blob], name, {type: mimes[ext] || blob.type});
    const share = {files: [file]};
    if (!navigator.canShare(share)) {
      toast("此文件无法直接分享，请先下载，再从手机文件管理器分享");
      return;
    }
    dialog({title: "分享文件", note: name + " · 在手机分享面板中选择微信", label: "打开分享面板",
      action: () => navigator.share(share)});
  } catch (error) {
    toast(error.message);
  }
}

function formatSize(bytes) {
  if (bytes < 1024) return bytes + " B";
  if (bytes < 1024 ** 2) return (bytes / 1024).toFixed(1) + " KB";
  return (bytes / 1024 ** 2).toFixed(1) + " MB";
}

function entryIcon(folder) {
  const ns = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(ns, "svg");
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("aria-hidden", "true");
  const outline = document.createElementNS(ns, "path");
  outline.setAttribute("fill", "none");
  outline.setAttribute("stroke-width", "1.6");
  outline.setAttribute("stroke-linejoin", "round");
  outline.setAttribute("d", folder ? "M3 6h7l2 2h9v11H3Z" : "M6 3h8l4 4v14H6Zm8 0v5h4");
  svg.append(outline);
  return svg;
}

function renderFiles() {
  const query = $("filter").value.trim().toLocaleLowerCase();
  const entries = state.rows.filter(row => row.name.toLocaleLowerCase().includes(query));
  $("entries").replaceChildren(...entries.map(row => {
    const path = [state.folder, row.name].filter(Boolean).join("/");
    const entry = node("article", undefined, "entry");
    const name = node("div", undefined, "entry-name");
    const detail = node("div");
    detail.append(row.folder ? button(row.name, () => openFolder(path)) : node("strong", row.name));
    if (!row.folder) detail.append(node("small", formatSize(row.size)));
    name.append(entryIcon(row.folder), detail);
    const actions = node("div", undefined, "entry-actions");
    if (!row.folder) {
      const download = node("a", "下载", "file-link");
      download.href = fileUrl(path);
      download.download = row.name;
      actions.append(download, button("分享", () => prepareShare(path, row.name, row.size)));
    }
    if (!row.drive) actions.append(button("改名", () => dialog({title: "修改名称", name: row.name, action: async value => {
      await post("/api/rename", {path, name: value});
      await openFolder(state.folder, true);
    }})), button("删除", () => dialog({title: "删除 " + row.name, note: "此项目将移至共享目录的回收文件夹。",
      label: "确认删除", action: async () => {
        await post("/api/delete", {path});
        await openFolder(state.folder, true);
      }})));
    entry.append(name, actions);
    return entry;
  }));
  if (!entries.length) $("entries").append(node("p", query ? "没有匹配的项目" : "这个文件夹是空的", "empty"));
}

async function openFolder(path, keepFilter = false) {
  const request = ++state.fileLoad;
  try {
    const result = await api(url("/api/files", {path}));
    if (request !== state.fileLoad) return;
    state.folder = result.path;
    state.rows = result.items;
    const driveList = state.allDrives && !path;
    for (const id of ["mkdir", "pick-upload", "folder-chat"]) $(id).hidden = driveList;
    $("choose-project").disabled = driveList;
    if (!keepFilter) $("filter").value = "";
    const parts = path ? path.split("/") : [];
    const trail = $("breadcrumbs");
    trail.replaceChildren(button(state.allDrives ? "此电脑" : "共享目录", () => openFolder("")));
    parts.forEach((part, index) => {
      trail.append(node("span", " / "), button(part, () => openFolder(parts.slice(0, index + 1).join("/"))));
    });
    renderFiles();
  } catch (error) {
    if (request === state.fileLoad) toast(error.message);
  }
}

$("login-form").addEventListener("submit", async event => {
  event.preventDefault();
  const submit = event.submitter;
  submit.disabled = true;
  $("login-error").textContent = "";
  try {
    await post("/api/login", {password: $("password").value});
    await enter(await api("/api/session"));
  } catch (error) {
    $("login-error").textContent = error.message;
  } finally {
    $("password").value = "";
    submit.disabled = false;
  }
});

$("composer").addEventListener("submit", async event => {
  event.preventDefault();
  const text = $("prompt").value.trim();
  if (!text || state.turnId || state.loading || state.sending) return;
  state.sending = true;
  showStatus("正在发送…");
  try {
    const effort = $("effort").value || null;
    const model = $("model").value || (effort ? selectedModel()?.id : null);
    const result = await post("/api/codex/send", {threadId: state.threadId, folder: state.chatFolder,
      projectId: state.chatProject?.id || null, model, effort, reviewer: $("reviewer").value, text});
    if ($("app").hidden) return;
    state.threadId = result.threadId;
    state.title = threadTitle(state.threads.find(thread => thread.id === state.threadId)) || text;
    if (state.title === "新建会话") state.title = text;
    if ($("files").hidden) $("title").textContent = state.title;
    state.turnId = result.turn.id;
    $("prompt").value = "";
    for (const item of result.turn.items || []) state.items.set(item.id, item);
    renderChat(true);
    renderThreads();
    showStatus("正在处理…");
  } catch (error) {
    showStatus("发送失败");
    toast(error.message);
  } finally {
    state.sending = false;
    showStatus();
  }
});

$("prompt").addEventListener("keydown", event => {
  if (event.key === "Enter" && (event.ctrlKey || event.metaKey) && !event.isComposing) {
    event.preventDefault();
    $("composer").requestSubmit();
  }
});

$("stop").onclick = () => post("/api/codex/stop", {threadId: state.threadId, turnId: state.turnId})
  .catch(error => toast(error.message));
$("new-chat").onclick = () => selectThread(null);
$("new-project").onclick = () => {
  state.pickingProject = true;
  $("project-picker").hidden = false;
  $("choose-project").disabled = true;
  tab(true);
  openFolder(state.folder, true);
};
$("cancel-project").onclick = () => {
  state.pickingProject = false;
  $("project-picker").hidden = true;
};
$("choose-project").onclick = () => {
  if (!state.pickingProject || (state.allDrives && !state.folder)) return;
  const folder = state.folder;
  const name = folder.split("/").pop() || state.root.split(/[\\/]/).filter(Boolean).pop() || "";
  dialog({title: "新建项目", name, note: folder || state.root, label: "创建项目", action: async value => {
    const project = await post("/api/codex/projects", {name: value, folder});
    $("cancel-project").click();
    state.projects = [...state.projects.filter(item => item.id !== project.id), project];
    selectThread(null, folder, {name: project.name, id: project.id});
    await reloadProjects();
  }});
};
$("retry-projects").onclick = reloadProjects;
$("folder-chat").onclick = () => selectThread(null, state.folder);
$("archived").onclick = () => {
  state.archived = !state.archived;
  $("archived").setAttribute("aria-pressed", String(state.archived));
  state.cursor = null;
  state.threads = [];
  renderThreads();
  loadThreads().catch(error => toast(error.message));
};
$("more").onclick = () => loadThreads(true).catch(error => toast(error.message));
$("open-sidebar").onclick = () => sidebar(true);
$("close-sidebar").onclick = $("scrim").onclick = () => sidebar(false);
$("show-chat").onclick = () => tab(false);
$("show-files").onclick = () => { tab(true); openFolder(state.folder, true); };
$("logout").onclick = async () => {
  try {
    await post("/api/logout", {});
    showLogin();
  } catch (error) {
    toast(error.message);
  }
};
$("refresh-files").onclick = () => openFolder(state.folder, true);
$("filter").oninput = renderFiles;
$("mkdir").onclick = () => {
  const path = state.folder;
  dialog({title: "新建文件夹", name: "", label: "创建", action: async name => {
    await post("/api/mkdir", {path, name});
    await openFolder(state.folder, true);
  }});
};
$("pick-upload").onclick = () => $("upload").click();
$("upload").onchange = async () => {
  const selected = [...$("upload").files];
  if (!selected.length) return;
  const path = state.folder;
  $("pick-upload").disabled = true;
  $("upload-status").hidden = false;
  const errors = [];
  for (const file of selected) {
    $("upload-status").textContent = "正在上传 " + file.name;
    try {
      if (file.size > 512 * 1024 * 1024) throw new Error("超过 512 MB");
      await api(url("/api/upload", {path, name: file.name}), {method: "POST", body: file});
    } catch (error) {
      errors.push(file.name + " · " + error.message);
    }
  }
  $("upload-status").hidden = true;
  $("upload").value = "";
  $("pick-upload").disabled = false;
  if (errors.length) toast(errors.join("\n"));
  else toast("上传完成");
  await openFolder(state.folder, true);
};
$("dialog-cancel").onclick = () => $("dialog").close();
$("dialog-form").onsubmit = async event => {
  event.preventDefault();
  const name = $("dialog-name").value;
  $("dialog").close();
  try {
    await dialogAction(name);
  } catch (error) {
    if (error.name !== "AbortError") toast(error.message);
  }
};

api("/api/session").then(enter).catch(showLogin);
poll();
