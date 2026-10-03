// Project path is supplied by Python; never print the private session cookie.
const task = await taskSpace(globalThis.bankDemoBrowserSpace || "银行客服真实模型页面核对");
const page = task.page("p1");
const fs = await import("node:fs/promises");
const path = await import("node:path");
const root = globalThis.bankDemoProjectRoot;
const journal = JSON.parse(await fs.readFile(path.join(root, "data/model-smoke-journal.json"), "utf8"));
const evidence = path.join(root, "docs/evidence/阶段2/真实模型");
await page.cdp("Network.setCookie", {
  name:"bank_demo_owner", value:journal.cookie, url:"http://localhost:18764", httpOnly:true, sameSite:"Strict",
});
async function ready() {
  await page.waitForFunction(() => !document.getElementById("send").disabled, undefined, {timeout:30000});
}
async function showCase(ident, customer) {
  await page.selectOption("loc=css:#customer", customer);
  await ready();
  const entry = journal.cases.find(item => item.id === ident);
  await page.selectOption("loc=css:#history", entry.session_id);
  await page.waitForFunction(question => !document.getElementById("send").disabled
    && document.getElementById("messages").textContent.includes(question), entry.question, {timeout:30000});
}
async function shot(name) {
  if (!await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)) throw Error("Horizontal overflow");
  await page.screenshot({path:path.join(evidence, name), fullPage:true});
}
await page.cdp("Emulation.setDeviceMetricsOverride", {width:1440, height:1100, deviceScaleFactor:1, mobile:false});
await page.goto("http://localhost:18764");
await showCase("M01", "personal");
await shot("01-真实个人开户.png");
await showCase("M04", "enterprise");
await page.cdp("Emulation.setDeviceMetricsOverride", {width:390, height:844, deviceScaleFactor:1, mobile:true});
await shot("02-真实企业开户手机.png");
await page.cdp("Emulation.setDeviceMetricsOverride", {width:1440, height:1100, deviceScaleFactor:1, mobile:false});
await showCase("M07", "personal");
await shot("03-真实无依据拒答.png");
console.log(await page.snapshot({scope:"full_page"}));
await task.finish({keep:[]});
