// Run via run_browser_check.py --version v13, against an isolated demonstration database.
const assert = (value, label) => { if (!value) throw new Error(label); };
const task = await taskSpace(globalThis.bankDemoBrowserSpace || "银行客服 V1.3 本机验收");
const page = task.page("p1");
async function waitFor(predicate, argument) {
  return page.waitForFunction(predicate, argument, {timeout:30000});
}
const fs = await import("node:fs/promises");
const path = await import("node:path");
const evidence = path.resolve(globalThis.bankDemoProjectRoot, "docs/evidence/阶段2/V1.3修订");
await fs.mkdir(evidence, {recursive:true});
console.log({spaceId:task.spaceId});
await page.cdp("Emulation.setDeviceMetricsOverride", {width:1440, height:1100, deviceScaleFactor:1, mobile:false});
await page.goto("http://127.0.0.1:18763");
await waitFor(() => document.getElementById("mode").textContent.includes("规则演示"));
console.log(await page.snapshot());

async function ready() {
  await waitFor(() => !document.getElementById("send").disabled);
}
async function navState(state) {
  await waitFor(value => document.getElementById("navigation").dataset.state === value, state);
}
async function services() {
  return page.evaluate(async () => {
    const sid = document.getElementById("history").value;
    const response = await fetch(`/api/v1/sessions/${sid}/services`);
    if (!response.ok) throw new Error("Service view could not be read");
    return response.json();
  });
}
async function ask(text) {
  await ready();
  const count = await page.evaluate(() => document.querySelectorAll(".message.assistant").length);
  await page.fill("#question", text);
  await page.click("#send");
  await waitFor(previous => !document.getElementById("send").disabled && document.querySelectorAll(".message.assistant").length > previous, count);
  return page.evaluate(() => document.querySelector(".message.assistant:last-child")?.textContent || "");
}
async function startFirst() {
  await ready();
  await page.click("#catalog button[data-capability] >> nth=0");
  await ready();
  await navState("reading");
}
async function shot(name) {
  assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), "Horizontal overflow");
  await page.screenshot({path:path.join(evidence,name), fullPage:true});
}
async function delayNextReply(suffix) {
  await page.evaluate(ending => {
    const original = window.fetch.bind(window);
    let next = true;
    window.v13Delayed = false;
    window.v13Released = false;
    window.fetch = async (...args) => {
      const response = await original(...args);
      if (next && String(args[0]).endsWith(ending) && args[1]?.method === "POST") {
        next = false;
        window.v13Delayed = true;
        await new Promise(resolve => { window.v13Release = resolve; });
        window.v13Released = true;
      }
      return response;
    };
  }, suffix);
}
async function releaseReply() {
  await page.evaluate(() => window.v13Release());
  await waitFor(() => window.v13Released);
  await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
}
async function delayNextNavigationRequest() {
  await page.evaluate(() => {
    const original = window.fetch.bind(window);
    let next = true;
    window.v13Delayed = false;
    window.v13Released = false;
    window.fetch = async (...args) => {
      if (next && String(args[0]).endsWith("/navigation") && args[1]?.method === "POST") {
        next = false;
        window.v13Delayed = true;
        await new Promise(resolve => { window.v13Release = resolve; });
        const response = await original(...args);
        window.v13Released = true;
        return response;
      }
      return original(...args);
    };
  });
}

console.log("CASE: catalog, cards and reading");
await page.selectOption("#customer", "personal");
await ready();
await page.click("#new");
await ready();
assert(await page.evaluate(() => document.querySelectorAll("#catalog button[data-capability]").length === 3), "Personal catalog must have three entries");
await startFirst();
let view = await services();
assert(view.navigation.step_index === 0, "Reading did not start at first paragraph");
assert(view.cards.every(card => card.customer_type === "personal"), "Wrong object in public cards");
await page.click("#nav-next");
await ready();
assert((await services()).navigation.step_index === 1, "Next did not advance reading");
await page.click("#nav-back");
await ready();
assert((await services()).navigation.step_index === 0, "Back did not restore first paragraph");
await shot("01-个人目录与公开卡片.png");

const suggestionCount = await page.evaluate(() => document.querySelectorAll("#suggestions button[data-suggestion]").length);
assert(suggestionCount > 0 && suggestionCount <= 3, "Bounded related questions missing");
const messageCount = await page.evaluate(() => document.querySelectorAll(".message.assistant").length);
await page.click("#suggestions button[data-suggestion] >> nth=0");
await waitFor(previous => !document.getElementById("send").disabled && document.querySelectorAll(".message.assistant").length > previous, messageCount);
assert((await services()).navigation.knowledge_id === view.navigation.knowledge_id, "Suggestion changed topic");
await page.click("#nav-next");
await ready();
await page.reload();
await ready();
assert((await services()).navigation.step_index === 1, "Refresh lost persisted paragraph");
await page.click("#nav-cancel");
await navState("cancelled");
await ready();
await page.reload();
await ready();
await navState("cancelled");
await shot("02-取消状态刷新恢复.png");

console.log("CASE: late reply after cancellation");
await startFirst();
await delayNextReply("/messages");
await page.fill("#question", "个人开户材料（迟到取消样例）");
await page.click("#send");
await waitFor(() => window.v13Delayed);
await page.click("#nav-cancel");
await navState("cancelled");
await releaseReply();
await ready();
assert((await services()).navigation.status === "cancelled", "Late message reopened cancelled navigation");
assert(await page.evaluate(() => document.getElementById("navigation").dataset.state === "cancelled"), "Late response revived reading UI");
await shot("03-迟到响应不恢复已取消导航.png");

console.log("CASE: late navigation response after object switch");
await startFirst();
await delayNextReply("/navigation");
await page.click("#nav-next");
await waitFor(() => window.v13Delayed);
await page.selectOption("#customer", "enterprise");
await ready();
await releaseReply();
assert(await page.evaluate(() => document.querySelectorAll("#catalog button[data-capability]").length === 3), "Enterprise catalog must have three entries");
view = await services();
assert(view.catalog.every(item => item.customer_type === "enterprise"), "Personal catalog leaked into enterprise context");
assert(view.cards.every(card => card.customer_type === "enterprise"), "Old personal card leaked into enterprise context");
await startFirst();
await shot("04-企业目录与切换隔离.png");

await page.selectOption("#customer", "personal");
await ready();
await startFirst();
console.log("CASE: rapid A-B-A with delayed cancellation delivery");
await delayNextNavigationRequest();
await page.selectOption("#customer", "enterprise");
await waitFor(() => window.v13Delayed);
await page.selectOption("#customer", "personal");
await releaseReply();
await ready();
await navState("cancelled");
assert((await services()).navigation.status === "cancelled", "Rapid A-B-A switch restored a task before its cancellation");
await page.selectOption("#customer", "enterprise");
await ready();

assert((await ask("查余额")).includes("未接入"), "Private query should stay unavailable");
assert((await services()).cards.length === 0, "Private query produced sample financial cards");
assert((await ask("转人工")).includes("未接入"), "Human channel should stay unavailable");
await ask("银行卡被盗刷了");
await startFirst();
await page.click("#nav-cancel");
await navState("cancelled");
await page.reload();
await ready();
assert(await page.evaluate(() => !document.getElementById("safety").hidden), "Navigation cancel or reload removed urgent notice");
await page.cdp("Emulation.setDeviceMetricsOverride", {width:390, height:844, deviceScaleFactor:1, mobile:true});
await startFirst();
await shot("05-手机卡片与独立紧急提示.png");

// Change only an explicitly supplied isolated copy; never modify the application's default catalog.
console.log("CASE: live isolated configuration changes");
if (globalThis.bankDemoTestCatalog) {
  const catalogFile = path.resolve(globalThis.bankDemoTestCatalog);
  assert(catalogFile !== path.resolve(globalThis.bankDemoProjectRoot, "backend/app/service_catalog.json"), "Refusing to modify the original catalog");
  const original = await fs.readFile(catalogFile, "utf8");
  const selected = (await services()).navigation.capability_id;
  try {
    const configuration = JSON.parse(original);
    const entries = Array.isArray(configuration) ? configuration : configuration.services || configuration.items;
    assert(Array.isArray(entries), "Unknown catalog configuration shape");
    const entry = entries.find(item => item.capability_id === selected);
    assert(entry, "Active capability not found in isolated catalog");
    entry.enabled = false;
    await fs.writeFile(catalogFile, JSON.stringify(configuration, null, 2));
    await page.reload();
    await ready();
    view = await services();
    assert(!view.catalog.some(item => item.capability_id === selected), "Disabled capability remains actionable");
    assert(view.navigation.status === "invalid", "Disabled knowledge left navigation active");
    assert(await page.evaluate(() => document.getElementById("nav-next").disabled), "Invalid navigation still exposes next action");
    await shot("06-停用能力与历史卡片失效.png");
    await fs.writeFile(catalogFile, "{invalid-json");
    assert((await ask("疑似诈骗")).includes("紧急"), "Broken catalog blocked urgent service");
    assert(await page.evaluate(() => !document.getElementById("safety").hidden), "Broken catalog hid public safety path");
  } finally {
    await fs.writeFile(catalogFile, original);
  }
  await page.reload();
  await ready();
}

console.log("PASS: desktop/mobile catalog, related questions, source cards, reading/back/cancel, refresh, three delay/switch races, independent safety and optional isolated configuration disable checks");
console.log(await page.snapshot({scope:"full_page"}));
await task.finish({keep:[]});
