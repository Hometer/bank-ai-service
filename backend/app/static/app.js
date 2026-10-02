"use strict";
const $ = id => document.getElementById(id);
try { $("customer").value = sessionStorage.getItem("bank_demo_customer") || ""; } catch { /* Optional browser preference only. */ }
let active = null;
let epoch = 0;
let waiting = false;
let safety = {active:false, revision:0};
let dismissing = false;
const kinds = {answered:"演示知识回答",unknown:"暂无依据",private_unavailable:"查询未接入",urgent:"紧急提示",human_unavailable:"人工/投诉未接入",sensitive_blocked:"敏感输入已屏蔽",model_unavailable:"模型未获得有效结果",clarification:"需要澄清公开主题"};

function renderSafety() {
  const valid = !safety.expires_at || Date.parse(safety.expires_at) > Date.now();
  $("safety").hidden = !safety.active || !valid;
  $("safety-text").textContent = safety.text || "";
  $("dismiss-safety").disabled = dismissing;
}
function mergeSafety(next) {
  const expired = safety.expires_at && Date.parse(safety.expires_at) <= Date.now();
  if (!next || (!expired && next.revision < safety.revision)) return;
  safety = next;
  if (next.active) $("safety-status").textContent = "";
  renderSafety();
}
async function refreshSafety() {
  try { mergeSafety(await api("/safety")); }
  catch (error) { if (error.status !== 401) $("safety-status").textContent = "紧急提示状态暂未同步，请使用已有官方渠道求助。"; }
}

async function api(path, options = {}) {
  const response = await fetch(`/api/v1${path}`, {
    ...options, headers: {"Content-Type":"application/json"}, signal: AbortSignal.timeout(35000),
  });
  const data = await response.json();
  if (!response.ok) {
    const error = new Error(data.error?.message || "请求失败，请重试。");
    error.status = response.status;
    throw error;
  }
  return data;
}

function setStatus(text) { $("status").textContent = text; }
function setBusy(value) { waiting = value; $("send").disabled = value || !active; }
function emptyMessages() {
  $("messages").replaceChildren();
  const p = document.createElement("p"); p.className = "empty";
  p.textContent = "输入一个非敏感问题，查看回答来源与服务状态。";
  $("messages").append(p);
}
function render(role, text, answer = null) {
  $("messages").querySelector(".empty")?.remove();
  const card = document.createElement("article"); card.className = `message ${role}`;
  const heading = document.createElement("h2"); heading.textContent = role === "user" ? "你 · 脱敏输入" : "智能客服 · 演示";
  const content = document.createElement("p"); content.textContent = text;
  card.append(heading, content);
  if (answer) {
    const state = document.createElement("span"); state.className = "kind";
    state.textContent = `${kinds[answer.kind] || answer.kind} · ${answer.channel_status}`;
    card.append(state);
    for (const source of answer.sources) {
      const item = document.createElement("div"); item.className = "source";
      item.textContent = `来源：${source.title} · ${source.customer_type === "enterprise" ? "企业" : source.customer_type === "personal" ? "个人" : "历史资料"} · ${source.version} · 演示知识，未获银行审核`;
      card.append(item);
    }
  }
  $("messages").append(card);
  $("messages").scrollTop = $("messages").scrollHeight;
}

async function loadSession(id, version) {
  const data = await api(`/sessions/${id}`);
  if (version !== epoch) return;
  active = data.session;
  emptyMessages();
  data.messages.forEach(message => render(message.role, message.text, message.answer));
  $("history").value = active.id;
  $("expiry").textContent = `本地演示会话保留至 ${new Date(active.expires_at).toLocaleString("zh-CN")}，到期后删除。`;
  await refreshSafety();
  if (version !== epoch) return;
  setBusy(false); setStatus("可以咨询 · 请仅使用演示数据");
}

async function refresh(forceNew = false) {
  const version = ++epoch;
  active = null; setBusy(true); emptyMessages(); setStatus("正在加载本机会话…");
  const type = $("customer").value;
  $("expiry").textContent = "";
  if (!type) {
    $("history").replaceChildren(); setBusy(false);
    setStatus("请先选择个人或企业公开咨询对象，再发送问题。");
    await refreshSafety(); return;
  }
  try {
    let sessions;
    try { sessions = await api(`/sessions?customer_type=${type}`); }
    catch (error) { if (error.status !== 401) throw error; sessions = []; }
    if (version !== epoch) return;
    if (forceNew || !sessions.length) {
      const session = await api("/sessions", {method:"POST", body:JSON.stringify({customer_type:type})});
      if (version !== epoch) return;
      sessions.unshift(session);
    }
    $("history").replaceChildren();
    for (const session of sessions) {
      const option = document.createElement("option"); option.value = session.id;
      option.textContent = `${new Date(session.created_at).toLocaleString("zh-CN")} · ${type === "personal" ? "个人" : "企业"}`;
      $("history").append(option);
    }
    await loadSession(sessions[0].id, version);
  } catch (error) {
    if (version !== epoch) return;
    setStatus(error.message); setBusy(false);
  }
}

$("form").addEventListener("submit", async event => {
  event.preventDefault();
  const text = $("question").value.trim();
  if (!text || !active || waiting) return;
  const version = epoch;
  const sid = active.id;
  setBusy(true); setStatus("正在处理…");
  $("question").value = "";
  try {
    const data = await api(`/sessions/${sid}/messages`, {method:"POST", body:JSON.stringify({text})});
    mergeSafety(data.safety);
    if (version !== epoch || active?.id !== sid) return;
    render("user", data.user_text);
    render("assistant", data.answer.text, data.answer);
    setStatus("已保存脱敏记录 · 真实模型验证待验");
  } catch (error) {
    if (version !== epoch) return;
    setStatus(`${error.message} 可刷新历史核对是否已保存，再重新输入。`);
  } finally { if (version === epoch) setBusy(false); }
});
$("customer").addEventListener("change", () => {
  try { sessionStorage.setItem("bank_demo_customer", $("customer").value); } catch { /* Consultation still works without browser storage. */ }
  refresh();
});
$("new").addEventListener("click", () => refresh(true));
$("history").addEventListener("change", async () => {
  const version = ++epoch; active = null; setBusy(true); emptyMessages();
  try { await loadSession($("history").value, version); }
  catch (error) { if (version === epoch) { setBusy(false); setStatus(error.message); } }
});
$("dismiss-safety").addEventListener("click", async () => {
  if (dismissing) return;
  dismissing = true; renderSafety();
  try {
    mergeSafety(await api("/safety/dismiss", {method:"POST", body:JSON.stringify({expected_revision:safety.revision})}));
    $("safety-status").textContent = safety.active ? "有新的紧急提示，请重新核对。" : "已关闭提示显示；未确认风险解除，也未办理挂失或冻结。";
  } catch (error) {
    await refreshSafety(); $("safety-status").textContent = error.message;
  } finally { dismissing = false; renderSafety(); }
});
setInterval(() => { renderSafety(); refreshSafety(); }, 60000);
document.querySelectorAll("[data-question]").forEach(button => button.addEventListener("click", () => {
  $("question").value = button.dataset.question; $("question").focus();
}));
(async () => {
  try {
    const data = await api("/health");
    $("mode").textContent = data.mode === "demo" ? "规则演示 · 非真实模型 · 模型验证待验" : "模型适配模式 · 真实效果验证待验";
    await refresh();
  } catch (error) { $("mode").textContent = "服务连接失败"; setStatus(error.message); }
})();
