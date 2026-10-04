"use strict";
const $ = id => document.getElementById(id);
try { $("customer").value = sessionStorage.getItem("bank_demo_customer") || ""; } catch { /* Optional browser preference only. */ }
let active = null;
let epoch = 0;
let waiting = false;
let services = null;
let consultation = null;
let cancelling = false;
const pendingCancellations = new Map();
let safety = {active:false, revision:0};
let dismissing = false;
const kinds = {answered:"演示知识回答",unknown:"暂无依据",private_unavailable:"查询未接入",urgent:"紧急提示",human_unavailable:"人工/投诉未接入",sensitive_blocked:"敏感输入已屏蔽",model_unavailable:"模型未获得有效结果",clarification:"需要澄清公开主题"};
const objectLabel = type => type === "enterprise" ? "企业" : type === "personal" ? "个人" : "历史资料";

function element(tag, text, className) {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = text;
  if (className) node.className = className;
  return node;
}
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
function renderConsultation(next) {
  consultation = next || null;
  const states = {idle:"",awaiting:"等待补充公开主题",exhausted:"已结束自动澄清，请选择目录、明确新问题或取消",cancelled:"普通咨询已取消，历史保留"};
  $("consultation-status").textContent = next ? `${states[next.status] || ""}${next.status === "awaiting" ? ` · 已提示 ${next.clarification_attempts}/2 次` : ""}` : "";
  updateControls();
}
const processingLabel = {rule:"规则演示",model:"真实模型判断",fallback:"模型未获得有效结果",legacy:"历史记录，处理方式未细分",knowledge:"知识库优先 · 本地资料检索"};
const coverageLabel = {overview:"演示概述",partial:"具体事项尚不能确认",none:"无可确认的业务依据",legacy:"历史资料范围未细分",supported:"已引用适用公开知识 · 请核对资料限制"};
function updateControls() {
  $("send").disabled = waiting || !active;
  const actions = new Set((services?.cards || []).filter(card => card.valid).flatMap(card => card.actions));
  $("nav-back").disabled = waiting || !active || !actions.has("back");
  $("nav-next").disabled = waiting || !active || !actions.has("next");
  $("nav-cancel").disabled = !active || cancelling || (!waiting && !actions.has("cancel") && !(["reading", "invalid"].includes(services?.navigation?.status)) && !consultation?.can_cancel);
  document.querySelectorAll("#catalog button, #suggestions button").forEach(button => { button.disabled = waiting || !active; });
}
function setBusy(value) { waiting = value; updateControls(); }
function emptyMessages() {
  const welcome = element("div", undefined, "empty welcome");
  welcome.append(element("div", "AI", "welcome-symbol"), element("h2", "您好，今天有什么可以帮您？"),
    element("p", "从开户准备到金融基础知识，\n为您梳理公开信息，并附上可核对的来源。"));
  const options = element("div", undefined, "welcome-options");
  const questions = $("customer").value === "enterprise" ? [
    ["企业开户准备", "企业开户需要什么材料"], ["网银与电子结单", "企业对账单在哪里下载"],
    ["企业融资说明", "企业融资有哪些用途"], ["外汇风险管理", "远期外汇是否稳赚"],
  ] : [["个人开户准备", "开户需要什么材料"], ["定期存款知识", "普通港元定存最低金额"],
    ["私人贷款材料", "自雇申请DreamCash需要什么材料"], ["了解存款保障", "每开一个账户都有80万元保障吗"]];
  for (const [label, question] of questions) {
    const button = element("button", label);
    button.type = "button"; button.append(element("span", "↗"));
    button.addEventListener("click", () => { $("question").value = question; $("question").focus(); });
    options.append(button);
  }
  welcome.append(options, element("p", "请先选择个人或企业服务；仅输入非敏感公开问题。", "welcome-note"));
  $("messages").replaceChildren(welcome);
}
function sourceText(source) {
  if (!source) return "来源缺失，此说明不可继续操作。";
  const publicKnowledge = source.status === "public_unreviewed";
  let value = `来源：${source.title} · ${objectLabel(source.customer_type)} · ${source.version} · ${publicKnowledge ? "公开知识整理" : "演示知识"}，未获银行审核`;
  if (publicKnowledge) value += ` · 核对日期：${source.retrieved_on || "未标明"} · 内部复核日：${source.review_due_on || "未标明"}（非银行有效期）`;
  else if (source.valid_from && source.valid_until) value += ` · 知识生效区间：${source.valid_from} 至 ${source.valid_until}`;
  return value;
}
function sourceNode(source) {
  const node = element("div", undefined, "source");
  node.append(element("div", "参考来源 · 公开资料整理", "source-heading"));
  const details = element("details");
  details.append(element("summary", "核对日期与资料详情"), element("p", sourceText(source)));
  const officialHosts = new Set(["hk.bankcomm.com","www.hk.bankcomm.com","eb.hk.bankcomm.com","www.bankcomm.com.hk","www.ifec.org.hk","www.dps.org.hk","www.info.gov.hk","www.news.gov.hk","www.hsbc.com.hk","www.hangseng.com","cms.hangseng.com","www.bochk.com","www.sc.com","www.hkbea.com","www.hkma.gov.hk"]);
  for (const ref of source?.references || []) {
    try {
      const url = new URL(ref.url);
      if (url.protocol !== "https:" || !officialHosts.has(url.hostname) || url.username || url.password) continue;
      const paragraph = element("p");
      const link = element("a", `${ref.title} ↗`);
      link.href = url.href; link.target = "_blank"; link.rel = "noopener noreferrer";
      paragraph.append(link, element("span", ref.publisher));
      node.append(paragraph);
      details.append(element("p", `出处定位：${ref.title} · ${ref.locator}`));
    } catch { /* Invalid historical links are never made actionable. */ }
  }
  node.append(details);
  return node;
}
function cardNode(card, historical = false) {
  const node = element("article", undefined, historical ? "history-card" : "service-card");
  node.dataset.cardId = card.id;
  node.dataset.valid = String(card.valid);
  node.dataset.capability = card.capability_id;
  node.append(element("h3", `${card.title} · ${objectLabel(card.customer_type)}公开咨询`));
  node.append(element("p", card.valid ? "演示说明 · 非银行办理结果" : "说明已失效 · 不可继续操作", "card-state"));
  if (historical || !card.valid || !card.steps?.length) {
    node.append(element("p", card.text));
  } else {
    node.append(element("p", `分步阅读 ${card.step_index + 1}/${card.steps.length} · 同一演示原文`, "reading-position"));
    node.append(element("p", card.steps[card.step_index] || card.text, "reading-text"));
    const details = element("details");
    details.append(element("summary", "查看完整演示说明"), element("p", card.text));
    node.append(details);
  }
  node.append(sourceNode(card.source));
  if (!card.valid) node.append(element("p", card.unavailable_reason || "来源或能力已不可用，请重新选择有效目录。", "card-unavailable"));
  if (historical) node.append(element("p", "历史说明仅供核对；继续咨询请使用当前有效目录。", "muted"));
  return node;
}
function render(role, text, answer = null) {
  $("messages").querySelector(".empty")?.remove();
  const card = element("article", undefined, `message ${role}`);
  card.append(element("h2", role === "user" ? "您 · 脱敏输入" : "AI 咨询助手"), element("p", text));
  if (answer) {
    const kind = answer.kind === "answered" && answer.processing === "knowledge" ? "公开知识回答" : (kinds[answer.kind] || answer.kind);
    card.append(element("span", `${kind} · ${answer.channel_status}`, "kind"));
    card.append(element("p", `${answer.processing ? processingLabel[answer.processing] : "既有版本，处理方式未细分"} · ${coverageLabel[answer.coverage || "legacy"]}`, "muted"));
    for (const part of answer.parts || []) {
      if ((answer.parts || []).length > 1) card.append(element("p", `${objectLabel(part.customer_type)}：${processingLabel[part.processing || "legacy"]} · ${coverageLabel[part.coverage || "legacy"]}`, "muted"));
    }
    for (const source of answer.sources) card.append(sourceNode(source));
    for (const item of answer.cards || []) {
      const history = element("details", undefined, "history-details");
      history.append(element("summary", item.valid ? "展开相关公开说明" : "查看已失效的历史说明"), cardNode(item, true));
      card.append(history);
    }
  }
  $("messages").append(card);
  $("messages").scrollTop = $("messages").scrollHeight;
}
function clearServices(message = "尚未开始阅读。") {
  services = null;
  renderConsultation(null);
  $("navigation").dataset.state = "idle";
  $("catalog").replaceChildren(element("p", active ? "正在核对可用目录…" : "选择个人或企业后查看可用目录。", "muted"));
  $("service-cards").replaceChildren();
  $("suggestions").replaceChildren();
  $("navigation-status").textContent = message;
  updateControls();
}
function renderServices(next) {
  if (!next) { clearServices("目录暂不可用，仍可输入公开求助问题。"); return; }
  if (services && next.session_revision < services.session_revision) return;
  services = next;
  renderConsultation(next.consultation);
  $("navigation").dataset.state = next.navigation.status;
  if (active) active.revision = Math.max(active.revision || 0, next.session_revision);
  $("catalog").replaceChildren();
  for (const item of next.catalog) {
    if (item.capability_id.includes("_faq_") && !/(?:_bocom|_hkma)$/.test(item.capability_id)) continue;
    const button = element("button");
    const paths = {opening:"M4 20h16M6 20V8m12 12V8M3 8l9-5 9 5H3M10 20V11m4 9V11",deposit:"M4 7h16v13H4V7M8 7V4h8v3M4 11h16M9 16h6",repayment:"M4 6h16v13H4V6M4 10h16M8 15h3",financing:"M4 20V10h5v10M9 20V6h5v14M14 20V3h5v17",reconciliation:"M6 3h12v18H6V3M9 8h6M9 12h6M9 16h4",borrowing:"M4 18L18 4M7 4a3 3 0 1 0 0 6 3 3 0 0 0 0-6M17 14a3 3 0 1 0 0 6 3 3 0 0 0 0-6",protection:"M12 3l7 3v5c0 5-7 10-7 10S5 16 5 11V6l7-3zM9 12l2 2 4-4",exchange:"M4 7h15l-4-4M20 17H5l4 4M19 7l-4 4M5 17l4-4"};
    const icon = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    icon.setAttribute("viewBox", "0 0 24 24"); icon.setAttribute("aria-hidden", "true");
    const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
    path.setAttribute("d", paths[item.capability_id.split("_").slice(1).join("_")] || paths.opening);
    icon.append(path);
    button.append(icon, element("span", item.capability_id.endsWith("_faq_hkma") ? "金融基础与支付知识" : item.title.replace("公开知识", "咨询").replace("知识", "")));
    button.type = "button"; button.dataset.capability = item.capability_id;
    button.dataset.selected = String(next.navigation.status === "reading" && item.capability_id === next.navigation.capability_id);
    button.addEventListener("click", () => navigate("start", item));
    $("catalog").append(button);
  }
  if (!next.catalog.length) $("catalog").append(element("p", "当前对象暂无有效演示目录。", "muted"));
  $("service-cards").replaceChildren();
  for (const card of next.cards) $("service-cards").append(cardNode(card));
  const states = {idle:"尚未开始阅读。请选择目录或输入问题。",reading:"正在阅读公开演示说明；读完不代表业务已办理。",cancelled:"普通导航已取消；历史说明保留。可重新选择目录开始。",invalid:"原说明已失效，请重新选择有效目录。"};
  $("navigation-status").textContent = states[next.navigation.status] || "请核对当前导航状态。";
  $("suggestions").replaceChildren();
  if (next.suggestions.length) $("suggestions").append(element("span", "相关演示问题", "muted"));
  for (const [index, item] of next.suggestions.slice(0, 3).entries()) {
    const button = element("button", item.question);
    button.type = "button"; button.dataset.suggestion = String(index);
    button.addEventListener("click", () => sendQuestion(item.question, item));
    $("suggestions").append(button);
  }
  updateControls();
}
async function serviceSnapshot(sid) { return api(`/sessions/${sid}/services`); }
async function syncHistory(sid, version) {
  const data = await api(`/sessions/${sid}`);
  if (version !== epoch || active?.id !== sid) return;
  active = data.session;
  emptyMessages();
  data.messages.forEach(message => render(message.role, message.text, message.answer));
  if (data.services) renderServices(data.services);
  else {
    try {
      const next = await serviceSnapshot(sid);
      if (version === epoch && active?.id === sid) renderServices(next);
    } catch (error) {
      if (version === epoch && active?.id === sid) clearServices(`${error.message} 仍可输入公开求助问题。`);
    }
  }
}
async function loadSession(id, version) {
  // A rapid A → B → A switch must not load A before its earlier cancellation
  // completes, otherwise its stale reading controls could be shown again.
  const pending = pendingCancellations.get(id);
  if (pending) {
    try { await pending; }
    catch (error) {
      if (version === epoch) $("transition-status").textContent = `此会话的取消状态未同步：${error.message} 请核对恢复状态。`;
    }
    if (version !== epoch) return;
  }
  const data = await api(`/sessions/${id}`);
  if (version !== epoch) return;
  active = data.session;
  emptyMessages();
  data.messages.forEach(message => render(message.role, message.text, message.answer));
  $("history").value = active.id;
  $("expiry").textContent = `本地演示会话保留至 ${new Date(active.expires_at).toLocaleString("zh-CN")}，到期后删除。`;
  if (data.services) renderServices(data.services);
  else {
    try {
      const next = await serviceSnapshot(id);
      if (version === epoch && active?.id === id) renderServices(next);
    } catch (error) {
      if (version === epoch) clearServices(`${error.message} 仍可输入公开求助问题。`);
    }
  }
  renderConsultation(data.consultation);
  await refreshSafety();
  if (version !== epoch) return;
  setBusy(false); setStatus("可以咨询 · 请仅使用演示数据");
}
// Retry a cancellation once after reading the newest server revision. This is
// also used for the previous session during an object/history switch.
async function performCancellation(session) {
  let revision = session.revision || 0;
  for (let attempt = 0; attempt < 2; attempt += 1) {
    try {
      return await api(`/sessions/${session.id}/navigation`, {method:"POST", body:JSON.stringify({action:"cancel",expected_revision:revision})});
    } catch (error) {
      if (error.status !== 409 || attempt === 1) throw error;
      // History remains readable when directory configuration is broken, so
      // cancellation can still recover a stale revision without that config.
      revision = (await api(`/sessions/${session.id}`)).session.revision;
    }
  }
}
function cancelSnapshot(session) {
  const existing = pendingCancellations.get(session.id);
  if (existing) return existing;
  const operation = performCancellation(session).finally(() => {
    if (pendingCancellations.get(session.id) === operation) pendingCancellations.delete(session.id);
  });
  pendingCancellations.set(session.id, operation);
  return operation;
}
async function cancelPrevious(session, version) {
  if (!session) return;
  try { await cancelSnapshot(session); }
  catch (error) {
    if (version === epoch) $("transition-status").textContent = `旧会话已停止展示，但取消状态未同步：${error.message} 请在历史会话核对。`;
  }
}
async function refresh(forceNew = false) {
  const previous = active ? {...active} : null;
  const version = ++epoch;
  active = null; cancelling = false; setBusy(true); emptyMessages(); clearServices(); setStatus("正在加载本机会话…");
  $("transition-status").textContent = "";
  const type = $("customer").value;
  $("expiry").textContent = "";
  await cancelPrevious(previous, version);
  if (version !== epoch) return;
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
      const option = element("option", `${new Date(session.created_at).toLocaleString("zh-CN")} · ${objectLabel(type)}`); option.value = session.id;
      $("history").append(option);
    }
    await loadSession(sessions[0].id, version);
  } catch (error) {
    if (version !== epoch) return;
    setStatus(error.message); setBusy(false);
  }
}
async function sendQuestion(text, suggestion = null) {
  if (!text || !active || waiting) return;
  const version = ++epoch;
  const sid = active.id;
  const body = {text,expected_revision:active.revision || 0};
  if (suggestion) { body.capability_id = suggestion.capability_id; body.knowledge_version = suggestion.knowledge_version; }
  setBusy(true); setStatus("正在处理…");
  $("question").value = "";
  try {
    const data = await api(`/sessions/${sid}/messages`, {method:"POST", body:JSON.stringify(body)});
    // Urgent events have their own monotonic revision and must survive a page
    // navigation even when that old conversation reply is no longer displayable.
    mergeSafety(data.safety);
    if (version !== epoch || active?.id !== sid) return;
    render("user", data.user_text);
    render("assistant", data.answer.text, data.answer);
    active.revision = Math.max(active.revision || 0, data.session_revision || 0);
    renderServices(data.services);
    renderConsultation(data.consultation);
    setStatus(`已保存脱敏记录 · ${processingLabel[data.answer.processing || "legacy"]}`);
  } catch (error) {
    if (version !== epoch || active?.id !== sid) return;
    if (error.status === 409) {
      try {
        const next = await serviceSnapshot(sid);
        if (version === epoch && active?.id === sid) renderServices(next);
      } catch { if (version === epoch) clearServices("目录状态暂不可用，请刷新核对。"); }
    }
    if (version === epoch) {
      if (!$("question").value) $("question").value = text;
      setStatus(`${error.message} 输入已保留；可刷新历史核对是否已保存后再发送。`);
    }
  } finally { if (version === epoch) setBusy(false); }
}
async function navigate(action, item = null) {
  if (!active || (waiting && action !== "cancel") || cancelling) return;
  const session = {...active};
  const version = ++epoch;
  cancelling = action === "cancel";
  setBusy(true);
  if (action === "cancel") {
    clearServices("已停止普通导航展示，正在同步取消状态…");
    setStatus("正在取消普通导航…");
  } else setStatus("正在核对来源与导航状态…");
  try {
    let next;
    if (action === "cancel") next = await cancelSnapshot(session);
    else {
      const body = {action,expected_revision:session.revision || 0};
      if (item) { body.capability_id = item.capability_id; body.knowledge_version = item.knowledge_version; }
      next = await api(`/sessions/${session.id}/navigation`, {method:"POST", body:JSON.stringify(body)});
    }
    if (version !== epoch || active?.id !== session.id) return;
    renderServices(next);
    if (action === "start") await syncHistory(session.id, version);
    if (version !== epoch || active?.id !== session.id) return;
    setStatus(action === "cancel" ? "普通导航已取消并保存 · 未关闭紧急提示" : "阅读位置已保存 · 不代表业务已办理");
  } catch (error) {
    if (version !== epoch || active?.id !== session.id) return;
    if (action === "cancel") clearServices(`已停止展示，取消状态未同步：${error.message} 请刷新核对。`);
    else {
      try {
        const next = await serviceSnapshot(session.id);
        if (version === epoch && active?.id === session.id) renderServices(next);
      } catch { if (version === epoch) clearServices("目录状态暂不可用，请刷新核对。"); }
    }
    if (version === epoch) setStatus(`${error.message} 请刷新历史核对。`);
  } finally {
    if (version === epoch) { cancelling = false; setBusy(false); }
  }
}
$("form").addEventListener("submit", event => { event.preventDefault(); sendQuestion($("question").value.trim()); });
$("nav-next").addEventListener("click", () => navigate("next"));
$("nav-back").addEventListener("click", () => navigate("back"));
$("nav-cancel").addEventListener("click", () => navigate("cancel"));
$("customer").addEventListener("change", () => {
  try { sessionStorage.setItem("bank_demo_customer", $("customer").value); } catch { /* Consultation still works without browser storage. */ }
  refresh();
});
$("new").addEventListener("click", () => refresh(true));
$("history").addEventListener("change", async () => {
  const id = $("history").value;
  const previous = active ? {...active} : null;
  const version = ++epoch;
  active = null; cancelling = false; setBusy(true); emptyMessages(); clearServices();
  $("transition-status").textContent = "";
  await cancelPrevious(previous, version);
  if (version !== epoch) return;
  try { await loadSession(id, version); }
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
const compactNavigation = matchMedia("(max-width: 767px)");
const syncNavigationWidth = () => { $("catalog-drawer").open = !compactNavigation.matches; };
syncNavigationWidth(); compactNavigation.addEventListener("change", syncNavigationWidth);
$("advisor-portrait").addEventListener("error", () => {
  $("advisor-portrait").hidden = true; $("portrait-fallback").hidden = false;
});
(async () => {
  try {
    const data = await api("/health");
    $("connection-status").textContent = "服务可用"; $("connection-dot").dataset.state = "available";
    $("knowledge-status").textContent = data.knowledge_base_status === "available" ? "知识库优先" : "公开咨询演示";
    const version = data.consultation_version === "1.4" ? "V1.4" : "既有版本";
    $("mode").textContent = data.mode === "demo" ? `${version} 规则演示 · 非真实模型` : `${version} 模型适配模式 · 每条答复标明实际处理方式`;
    if (data.knowledge_base_status === "available") {
      $("mode").textContent = "交通银行（香港）知识库优先 · 本地检索，无模型调用";
      if (Number.isInteger(data.knowledge_article_count) && Number.isInteger(data.knowledge_bank_count)) {
        $("knowledge-status").textContent = `知识库优先 · ${data.knowledge_article_count} 条`;
        $("mode").textContent = `交银香港客服 · ${data.knowledge_article_count} 条公开知识及参考建议 · 本地检索，无模型调用`;
        $("bank-knowledge-hint").textContent = "以交通银行（香港）为咨询主体；同业资料仅作通用参考，具体条件以交银指引为准。";
      }
    }
    if (data.knowledge_base_status === "unavailable") {
      $("mode").textContent += " · 公开知识资料不可用或需复核";
      $("knowledge-status").textContent = "知识资料待核对";
    }
    await refresh();
  } catch (error) {
    $("mode").textContent = "服务连接失败"; $("connection-status").textContent = "连接失败";
    $("connection-dot").dataset.state = "failed"; $("knowledge-status").textContent = "暂未核对知识资料";
    setStatus(error.message);
  }
})();
