// Run through scripts/run_browser_check.py, which supplies the project path and optional space id.
const assert = (value, label) => { if (!value) throw new Error(label); };
const task = await taskSpace(globalThis.bankDemoBrowserSpace || "银行客服 V1.2 本机验收");
const page = task.page("p1");
const fs = await import("node:fs/promises");
const path = await import("node:path");
const evidence = path.resolve(globalThis.bankDemoProjectRoot, "docs/evidence/阶段2/V1.2修订");
await fs.mkdir(evidence, {recursive:true});
console.log({spaceId:task.spaceId});
await page.cdp("Emulation.setDeviceMetricsOverride", {width:1440, height:1100, deviceScaleFactor:1, mobile:false});
await page.goto("http://127.0.0.1:18763");
await page.waitForFunction(() => document.getElementById("mode").textContent.includes("规则演示"));
console.log(await page.snapshot());

async function ready() {
  await page.waitForFunction(() => !document.getElementById("send").disabled);
}
async function ask(text) {
  await ready();
  await page.fill("#question", text);
  await page.click("#send");
  await page.waitForFunction(() => !document.getElementById("send").disabled && document.getElementById("status").textContent.includes("已保存"));
  return page.evaluate(() => document.querySelector(".message.assistant:last-child")?.textContent || "");
}
async function delayNextReply() {
  await page.evaluate(() => {
    const original = window.fetch.bind(window);
    let next = true;
    window.v12Delayed = false;
    window.v12Released = false;
    window.fetch = async (...args) => {
      const response = await original(...args);
      if (next && String(args[0]).endsWith("/messages") && args[1]?.method === "POST") {
        next = false;
        window.v12Delayed = true;
        await new Promise(resolve => { window.v12Release = resolve; });
        window.v12Released = true;
      }
      return response;
    };
  });
}
async function releaseReply() {
  await page.evaluate(() => window.v12Release());
  await page.waitForFunction(() => window.v12Released);
  // Let response.json and the application's handlers finish before asserting display state.
  await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
}
async function screenshot(name) {
  await page.screenshot({path:path.join(evidence, name), fullPage:true});
}

await page.selectOption("#customer", "");
await page.waitForFunction(() => document.getElementById("status").textContent.includes("请先选择"));
assert(await page.evaluate(() => document.getElementById("send").disabled), "An unselected object must require clarification");
await page.selectOption("#customer", "personal");
await ready();
await page.click("#new");
await ready();
assert((await ask("那需要什么")).includes("需要澄清"), "Missing topic must not be guessed");
assert((await ask("开户需要什么材料")).includes("个人开户咨询"), "Personal source missing");
assert((await ask("那需要什么")).includes("个人开户咨询"), "Valid topic did not continue");
assert((await ask("企业开户材料")).includes("企业公开咨询"), "Explicit cross-object answer missing label");
assert(await page.evaluate(() => document.getElementById("customer").value === "personal"), "Public answer changed session identity");
const mixed = await ask("个人贷款和企业融资都要哪些材料");
assert(mixed.includes("个人贷款咨询") && mixed.includes("企业融资咨询"), "Mixed question lost independent sources");
await screenshot("01-桌面混合公开咨询.png");
for (const text of ["忘记密码怎么办", "验证码收不到", "2026年资料哪里查"]) {
  assert(!(await ask(text)).includes("敏感输入已屏蔽"), "Harmless public question was blocked");
}
await ask("验证码是123456");
assert(!(await page.evaluate(() => document.getElementById("messages").textContent)).includes("123456"), "Credential appeared in history");
await screenshot("02-正常咨询与凭证屏蔽.png");

await ask("银行卡被盗刷了");
assert(await page.evaluate(() => !document.getElementById("safety").hidden), "Urgent notice missing");
await ask("开户材料");
await page.selectOption("#customer", "enterprise");
await ready();
await page.reload();
await ready();
assert(await page.evaluate(() => !document.getElementById("safety").hidden), "Switch or refresh removed urgent notice");
await page.cdp("Emulation.setDeviceMetricsOverride", {width:390, height:844, deviceScaleFactor:1, mobile:true});
assert(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth), "Mobile horizontal overflow");
await screenshot("03-手机紧急提示恢复.png");
await page.click("#dismiss-safety");
await page.waitForFunction(() => document.getElementById("safety").hidden);
assert((await page.evaluate(() => document.getElementById("safety-status").textContent)).includes("未确认风险解除"), "Dismissal claimed risk resolved");
await page.reload();
await ready();
assert(await page.evaluate(() => document.getElementById("safety").hidden), "Reload revived a dismissed notice");

await page.cdp("Emulation.setDeviceMetricsOverride", {width:1440, height:1100, deviceScaleFactor:1, mobile:false});
await page.selectOption("#customer", "personal");
await ready();
await page.click("#new");
await ready();
await delayNextReply();
await page.fill("#question", "个人开户材料（迟到响应样例）");
await page.click("#send");
await page.waitForFunction(() => window.v12Delayed);
await page.selectOption("#customer", "enterprise");
await ready();
await releaseReply();
assert(await page.evaluate(() => !document.getElementById("messages").textContent.includes("迟到响应样例")), "Late response polluted new object");
assert(await page.evaluate(() => document.getElementById("customer").value === "enterprise" && !document.getElementById("send").disabled), "Late response changed current controls");
await screenshot("04-迟到响应未进入企业语境.png");

await ask("疑似诈骗");
await delayNextReply();
await page.fill("#question", "企业开户材料");
await page.click("#send");
await page.waitForFunction(() => window.v12Delayed);
await page.click("#dismiss-safety");
await page.waitForFunction(() => document.getElementById("safety").hidden);
await releaseReply();
await ready();
assert(await page.evaluate(() => document.getElementById("safety").hidden), "Old normal response revived dismissed urgent notice");
await screenshot("05-旧响应不复活已关闭提示.png");
await ask("疑似诈骗");
assert(await page.evaluate(() => !document.getElementById("safety").hidden), "New urgent event failed to reactivate");
assert(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth), "Desktop horizontal overflow");
console.log("PASS: desktop/mobile, object clarification, public sources, redaction, persisted notice and two real-response delay scenarios");
console.log(await page.snapshot({scope:"full_page"}));
await task.finish({keep:[]});
