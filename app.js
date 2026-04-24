"use strict";

const state = {
  contacts: [],
  selectedContactId: null,
  selectedSessionIndex: null,
};

let logSequence = 0;

const els = {
  runtimeText: document.getElementById("runtimeText"),
  progressFill: document.getElementById("progressFill"),
  progressText: document.getElementById("progressText"),
  manualDirBlock: document.getElementById("manualDirBlock"),
  showManualDirBtn: document.getElementById("showManualDirBtn"),
  manualDirForm: document.getElementById("manualDirForm"),
  manualDir: document.getElementById("manualDir"),
  parseBtn: document.getElementById("parseBtn"),
  loadContactsBtn: document.getElementById("loadContactsBtn"),
  contactSearch: document.getElementById("contactSearch"),
  contactList: document.getElementById("contactList"),
  resultEmpty: document.getElementById("resultEmpty"),
  resultContent: document.getElementById("resultContent"),
  resultName: document.getElementById("resultName"),
  resultRange: document.getElementById("resultRange"),
  resultWinner: document.getElementById("resultWinner"),
  resultWinnerMeta: document.getElementById("resultWinnerMeta"),
  messageCount: document.getElementById("messageCount"),
  resultCounts: document.getElementById("resultCounts"),
  compareSummary: document.getElementById("compareSummary"),
  selfShareBar: document.getElementById("selfShareBar"),
  otherShareBar: document.getElementById("otherShareBar"),
  selfShareText: document.getElementById("selfShareText"),
  otherShareText: document.getElementById("otherShareText"),
  sessionTableBody: document.getElementById("sessionTableBody"),
};

els.parseBtn.addEventListener("click", runOneClickParse);
els.showManualDirBtn.addEventListener("click", () => {
  els.manualDirForm.classList.toggle("hidden");
});
els.loadContactsBtn.addEventListener("click", loadContacts);
els.contactSearch.addEventListener("input", renderContacts);

boot();

async function boot() {
  try {
    await logClientEvent("page_boot");
    await fetch("/api/health", { cache: "no-store" });
    await refreshRuntime();
  } catch (error) {
    await logClientEvent("boot_failed", { message: String(error && error.message || error) });
    els.runtimeText.textContent = "本地程序还没有启动成功，请先打开程序后再试";
  }
}

async function refreshRuntime() {
  const res = await fetch("/api/wechat/runtime", { cache: "no-store" });
  const payload = await res.json();
  await logClientEvent("runtime_refreshed", {
    accountCount: payload.account_count || 0,
    decryptedReady: Boolean(payload.decrypted_ready),
    problems: payload.problems || [],
  });
  const problems = payload.problems || [];
  els.runtimeText.textContent = problems.length
    ? "还差一点环境准备，先按下面提示处理"
    : payload.decrypted_ready
      ? "已检测到可用数据库，可以直接选联系人"
      : payload.account_count
        ? "已找到微信聊天目录，直接点“解析本地聊天记录”就行"
        : "先打开并登录微信，再点“解析本地聊天记录”";
  els.manualDirBlock.classList.toggle("hidden", payload.account_count > 0);
}

async function runOneClickParse() {
  const manualDir = els.manualDir.value.trim();
  await logClientEvent("parse_clicked", { hasManualDir: Boolean(manualDir), manualDir });
  const healthOk = await ensureBackendReady("解析前检查失败，本地程序可能已经退出了，请重新打开后再试");
  if (!healthOk) {
    return;
  }
  const params = new URLSearchParams();
  if (manualDir) {
    params.set("manual_dir", manualDir);
  }
  setProgress(10, "正在检查微信是否已登录...");
  const progressMessages = [
    { delay: 400, percent: 28, text: "正在查找本机微信聊天记录目录..." },
    { delay: 1100, percent: 52, text: "正在提取解密所需信息..." },
    { delay: 2100, percent: 78, text: "正在解密并读取联系人..." },
  ];
  const timers = progressMessages.map((item) =>
    setTimeout(() => setProgress(item.percent, item.text), item.delay),
  );
  let payload;
  try {
    const res = await fetch(`/api/wechat/parse?${params.toString()}`, { method: "POST" });
    payload = await res.json();
  } catch (error) {
    timers.forEach((timer) => clearTimeout(timer));
    await logClientEvent("parse_request_failed", { message: String(error && error.message || error) });
    setProgress(0, "解析失败，本地程序可能已经退出了，请重新打开后再试");
    return;
  }
  await logClientEvent("parse_response", {
    ok: Boolean(payload.ok),
    error: payload.error || "",
    reason: payload.reason || "",
    reasonDetail: payload.reason_detail || "",
    reasonLabel: payload.reason_label || "",
    tips: payload.tips || [],
    contactCount: payload.contact_count || 0,
  });
  timers.forEach((timer) => clearTimeout(timer));
  if (payload.ok) {
    setProgress(100, "解析完成，可以直接选择联系人了");
  } else {
    const firstTip = payload.tips && payload.tips.length ? payload.tips[0] : "";
    const failedText = payload.reason_label || firstTip || "请先登录微信后再试";
    setProgress(0, `解析失败，${failedText}`);
  }
  await refreshRuntime();
  if (payload.ok) {
    await loadContacts();
  } else if (payload.show_manual_dir) {
    els.manualDirBlock.classList.remove("hidden");
  }
}

async function ensureBackendReady(fallbackMessage) {
  try {
    const res = await fetch("/api/health", { cache: "no-store" });
    if (!res.ok) {
      throw new Error(`health ${res.status}`);
    }
    return true;
  } catch (error) {
    await logClientEvent("health_check_failed", { message: String(error && error.message || error) });
    els.runtimeText.textContent = fallbackMessage;
    setProgress(0, fallbackMessage);
    return false;
  }
}

async function loadContacts() {
  try {
    const res = await fetch("/api/wechat/contacts", { cache: "no-store" });
    const payload = await res.json();
    const items = payload.items || [];
    await logClientEvent("contacts_loaded", { count: items.length });
    state.contacts = items.map((item) => ({
      id: item.username,
      name: item.display_name,
      username: item.username,
      loaded: false,
      sessions: [],
      selfStarts: 0,
      otherStarts: 0,
      range: "-",
    }));
    state.selectedContactId = null;
    state.selectedSessionIndex = null;
    if (!items.length) {
      els.runtimeText.textContent = "这次没有读到可用联系人，请重新点一次解析";
    }
    renderContacts();
    renderResult();
  } catch (error) {
    await logClientEvent("contacts_load_failed", { message: String(error && error.message || error) });
    els.runtimeText.textContent = "联系人列表加载失败，请重新点一次解析";
    state.contacts = [];
    state.selectedContactId = null;
    state.selectedSessionIndex = null;
    renderContacts();
    renderResult();
  }
}

function renderContacts() {
  const keyword = els.contactSearch.value.trim().toLowerCase();
  const contacts = state.contacts.filter((item) => item.name.toLowerCase().includes(keyword));
  if (!contacts.length) {
    els.contactList.className = "card-list empty-state";
    els.contactList.textContent = state.contacts.length ? "没有匹配联系人。" : "解密后再加载联系人。";
    return;
  }

  els.contactList.className = "card-list";
  els.contactList.innerHTML = contacts.map((contact) => `
    <button type="button" class="contact-item ${state.selectedContactId === contact.id ? "active" : ""}" data-id="${escapeHtml(contact.id)}">
      <strong>${escapeHtml(contact.name)}</strong>
    </button>
  `).join("");

  Array.from(els.contactList.querySelectorAll(".contact-item")).forEach((button) => {
    button.addEventListener("click", async () => {
      await logClientEvent("contact_selected", { username: button.dataset.id });
      state.selectedContactId = button.dataset.id;
      state.selectedSessionIndex = null;
      renderContacts();
      await analyzeContact(state.selectedContactId);
    });
  });
}

function setProgress(percent, text) {
  els.progressFill.style.width = `${Math.max(0, Math.min(percent, 100))}%`;
  els.progressText.textContent = text;
}

async function analyzeContact(username) {
  await logClientEvent("analyze_clicked", { username });
  const res = await fetch(`/api/wechat/analyze?username=${encodeURIComponent(username)}`, { cache: "no-store" });
  const payload = await res.json();
  await logClientEvent("analyze_response", {
    username,
    ready: Boolean(payload.ready),
    messageCount: payload.message_count || 0,
    sessionCount: payload.session_count || 0,
  });
  const index = state.contacts.findIndex((item) => item.id === username);
  if (index < 0) {
    return;
  }
  state.contacts[index] = {
    ...state.contacts[index],
    loaded: payload.ready,
    sessions: (payload.sessions || []).map((session) => ({
      startTimestamp: new Date(session.start_timestamp * 1000),
      firstSenderRole: session.first_sender_role,
      firstSenderName: session.first_sender_label,
      firstContent: prettyMessageText(session.first_text),
      messages: (session.messages || []).map((message) => ({
        timestamp: new Date(message.timestamp * 1000),
        senderRole: message.sender_role,
        sender: message.sender_label,
        content: prettyMessageText(message.text),
      })),
    })),
    selfStarts: payload.self_starts || 0,
    otherStarts: payload.other_starts || 0,
    messageCount: payload.message_count || 0,
    range: payload.range ? `${payload.range.from.slice(0, 10)} - ${payload.range.to.slice(0, 10)}` : "-",
  };
  renderResult();
  renderContacts();
}

function renderResult() {
  const contact = state.contacts.find((item) => item.id === state.selectedContactId);
  if (!contact || !contact.loaded) {
    els.resultEmpty.classList.remove("hidden");
    els.resultContent.classList.add("hidden");
    els.resultEmpty.textContent = state.selectedContactId ? "正在加载这个联系人的分析结果..." : "选中联系人后，这里会显示分析结果。";
    return;
  }
  els.resultEmpty.classList.add("hidden");
  els.resultContent.classList.remove("hidden");

  els.resultName.textContent = contact.name;
  els.resultRange.textContent = contact.range;
  els.messageCount.textContent = String(contact.messageCount || 0);
  els.resultCounts.textContent = `你主动 ${contact.selfStarts} 次 · TA 主动 ${contact.otherStarts} 次`;
  const totalStarts = contact.selfStarts + contact.otherStarts;
  const selfShare = totalStarts ? Math.round((contact.selfStarts / totalStarts) * 100) : 50;
  const otherShare = totalStarts ? 100 - selfShare : 50;
  els.selfShareBar.style.width = `${selfShare}%`;
  els.otherShareBar.style.width = `${otherShare}%`;
  els.selfShareText.textContent = `${selfShare}%`;
  els.otherShareText.textContent = `${otherShare}%`;

  if ((contact.messageCount || 0) < 50) {
    els.resultWinner.textContent = "火候还不够";
    els.resultWinnerMeta.textContent = "消息还不到 50 条，先别急着认领舔狗位";
    els.compareSummary.textContent = "再聊一阵子，剧情才会展开";
  } else if (selfShare > 90) {
    els.resultWinner.textContent = "你是超级大舔狗";
    els.resultWinnerMeta.textContent = "再舔一口吧，也没什么的";
    els.compareSummary.textContent = `你的主动占比已经冲到 ${selfShare}% 了`;
  } else if (otherShare > 90) {
    els.resultWinner.textContent = "TA是超级大舔狗";
    els.resultWinnerMeta.textContent = "偶尔让TA舔一口又何妨呢";
    els.compareSummary.textContent = `TA 的主动占比已经冲到 ${otherShare}% 了`;
  } else if (selfShare > 60) {
    els.resultWinner.textContent = "你是舔狗";
    els.resultWinnerMeta.textContent = `主动占比高达 ${selfShare}%，这波你是真上头`;
    els.compareSummary.textContent = "这段关系里，你明显更放不下";
  } else if (otherShare > 60) {
    els.resultWinner.textContent = "TA是舔狗";
    els.resultWinnerMeta.textContent = `主动占比高达 ${otherShare}%，TA 对你是真的惦记`;
    els.compareSummary.textContent = "这段关系里，TA 明显更上头";
  } else {
    els.resultWinner.textContent = "你们是最好的朋友！";
    els.resultWinnerMeta.textContent = "还有什么是比这更好的呢";
    els.compareSummary.textContent = "谁也没在硬舔，刚刚好就是最舒服";
  }

  els.sessionTableBody.innerHTML = contact.sessions.map((session, index) => renderSessionRow(session, index)).join("");
  els.sessionTableBody.onclick = (event) => {
    const button = event.target.closest(".session-detail-btn");
    if (!button) {
      return;
    }
    const index = Number(button.dataset.index);
    logClientEvent("session_toggle", { index, opened: state.selectedSessionIndex !== index });
    state.selectedSessionIndex = state.selectedSessionIndex === index ? null : index;
    renderResult();
  };
}

async function logClientEvent(event, details = {}) {
  logSequence += 1;
  const payload = {
    event,
    details: {
      ...details,
      seq: logSequence,
      page: "main",
      selectedContactId: state.selectedContactId || "",
      timestamp: new Date().toISOString(),
    },
  };
  try {
    await fetch("/api/log", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
  } catch (error) {
    // Ignore logging failures so they never break the main flow.
  }
}

function renderSessionRow(session, index) {
  const isOpen = state.selectedSessionIndex === index;
  const summary = `
    <tr class="${isOpen ? "session-row-open" : ""}">
      <td>${formatDateTime(session.startTimestamp)}</td>
      <td>${session.firstSenderRole === "self" ? "你" : escapeHtml(session.firstSenderName)}</td>
      <td>${escapeHtml(session.firstContent || "(无文本内容)")}</td>
      <td>${session.messages.length}</td>
      <td><button type="button" class="ghost-btn session-detail-btn" data-index="${index}">${isOpen ? "收起详情" : "查看详情"}</button></td>
    </tr>
  `;
  if (!isOpen) {
    return summary;
  }
  const detail = session.messages.map((message) => `
    <article class="session-message ${message.senderRole === "self" ? "self" : "other"}">
      <div class="session-message-meta">
        <strong>${escapeHtml(message.senderRole === "self" ? "你" : message.sender || "对方")}</strong>
        <span>${formatDateTime(message.timestamp)}</span>
      </div>
      <div class="session-message-content">${escapeHtml(message.content || "(无文本内容)")}</div>
    </article>
  `).join("");
  return `${summary}<tr class="session-detail-row"><td colspan="5"><div class="inline-session-detail">${detail}</div></td></tr>`;
}

function prettyMessageText(text) {
  text = String(text || "").trim();
  if (!text) {
    return "";
  }
  if (/^\[(图片|表情|动画表情|语音|视频|文件|链接|小程序|位置|卡片)([:：].*)?\]$/u.test(text)) {
    const match = text.match(/^\[([^\]:：]+).*?\]$/u);
    return match ? `[${match[1]}]` : text;
  }
  if (looksLikeXml(text)) {
    return summarizeXml(text);
  }
  if (looksLikeJson(text)) {
    return summarizeJson(text);
  }
  return text;
}

function looksLikeXml(text) {
  return text.startsWith("<") && text.includes(">");
}

function looksLikeJson(text) {
  return (text.startsWith("{") && text.endsWith("}")) || (text.startsWith("[") && text.endsWith("]"));
}

function summarizeXml(text) {
  const title = decodeXml(extractXmlTag(text, "title"));
  const description = decodeXml(extractXmlTag(text, "des"));
  const type = extractXmlTag(text, "type");
  const appName = decodeXml(extractXmlTag(text, "appname"));
  if (/imgbuf|cdnthumburl|<img\b|<image\b/i.test(text)) return "[图片]";
  if (/videomsg|<video\b/i.test(text)) return "[视频]";
  if (/voicemsg|<voice\b/i.test(text)) return "[语音]";
  if (/appmsg|recorditem/i.test(text)) {
    if (type === "19" || /聊天记录|合并转发/u.test(title + description)) return title ? `[合并转发] ${title}` : "[合并转发消息]";
    if (/文件/u.test(title + description) || type === "6") return title ? `[文件] ${title}` : "[文件]";
    if (/小程序/u.test(appName + title) || type === "33") return title ? `[小程序] ${title}` : "[小程序]";
    if (title) return appName ? `[${appName}] ${title}` : title;
  }
  return title || "[结构化消息]";
}

function summarizeJson(text) {
  try {
    const data = JSON.parse(text);
    const title =
      pickSummary(data, ["title", "summary", "desc", "description", "prompt", "text", "content"]) ||
      pickSummary(data.meta, ["title", "summary"]) ||
      pickSummary(data.appmsg, ["title", "des"]);
    if (title) {
      return /聊天记录|合并转发/u.test(title) ? `[合并转发] ${title}` : title;
    }
  } catch (error) {
    return "[结构化消息]";
  }
  return "[结构化消息]";
}

function pickSummary(source, keys) {
  if (!source || typeof source !== "object") {
    return "";
  }
  for (const key of keys) {
    const value = source[key];
    if (typeof value === "string" && value.trim()) {
      return value.trim();
    }
  }
  return "";
}

function extractXmlTag(text, tagName) {
  const match = text.match(new RegExp(`<${tagName}>([\\s\\S]*?)</${tagName}>`, "i"));
  return match ? match[1].trim() : "";
}

function decodeXml(text) {
  return String(text || "")
    .replace(/&lt;/g, "<")
    .replace(/&gt;/g, ">")
    .replace(/&amp;/g, "&")
    .replace(/&quot;/g, '"')
    .replace(/&#39;/g, "'");
}

function formatDateTime(value) {
  if (!(value instanceof Date) || Number.isNaN(value.getTime())) {
    return "-";
  }
  const y = value.getFullYear();
  const m = String(value.getMonth() + 1).padStart(2, "0");
  const d = String(value.getDate()).padStart(2, "0");
  const h = String(value.getHours()).padStart(2, "0");
  const min = String(value.getMinutes()).padStart(2, "0");
  return `${y}-${m}-${d} ${h}:${min}`;
}

function escapeHtml(value) {
  return String(value || "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}
