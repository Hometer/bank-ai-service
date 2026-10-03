// Run against a real local knowledge preview; no model or bank calls.
export async function verify(page, root) {
  const report = [];
  const assert = (value, message) => { if (!value) throw new Error(message); };
  const ready = () => page.waitForFunction(() => !document.querySelector('#send').disabled);
  const ask = async text => {
    await ready();
    await page.fill('#question', text); await page.click('#send');
    await page.waitForFunction(() => !document.querySelector('#send').disabled && document.querySelector('#status').textContent.includes('已保存'));
    return page.evaluate(() => document.querySelector('#messages').lastElementChild.textContent);
  };
  await page.evaluate(() => {
    window.financialUiErrors = [];
    addEventListener('error', event => { if (event.message) window.financialUiErrors.push(event.message); });
    addEventListener('unhandledrejection', event => window.financialUiErrors.push(String(event.reason)));
  });
  await page.click('#messages .source details summary >> nth=0');
  assert(await page.evaluate(() => document.querySelector('#messages .source details').open), 'Source details did not open');
  await page.click('#messages .history-details>summary >> nth=0');
  assert(await page.evaluate(() => document.querySelector('#messages .history-details').open), 'History explanation inaccessible');
  await page.click('#messages .history-details>summary >> nth=0');
  await page.click('#messages .source details summary >> nth=0');
  report.push('Source and history disclosures');
  await page.click('#nav-next');
  await page.waitForFunction(() => document.querySelector('#service-cards').textContent.includes('分步阅读 2/'));
  // The high-level reload command can wait on an unrelated browser lifecycle;
  // CDP dispatch plus an application condition verifies the real persisted state.
  await page.cdp('Page.reload', {ignoreCache:true});
  await ready();
  await page.waitForFunction(() => document.querySelector('#service-cards').textContent.includes('分步阅读 2/'));
  await page.click('#nav-back');
  await page.waitForFunction(() => document.querySelector('#service-cards').textContent.includes('分步阅读 1/'));
  await page.click('#nav-cancel');
  await page.waitForFunction(() => document.querySelector('#navigation').dataset.state === 'cancelled');
  report.push('Reading next/back/cancel and real refresh recovery');
  await page.selectOption('#customer', 'enterprise'); await ready();
  await page.click('#new'); await ready();
  assert((await ask('企业对账单在哪里下载，需要什么权限')).includes('认证及相应账户权限'), 'Enterprise source reply missing');
  assert(await page.evaluate(() => document.querySelectorAll('#catalog button').length === 6), 'Enterprise catalog count changed');
  report.push('Enterprise selection/new conversation/knowledge sources');
  for (const [width,height,name] of [[1280,900,'03-1280企业咨询.png'],[768,1024,'04-平板布局.png'],[390,844,'05-手机首页.png']]) {
    await page.cdp('Emulation.setDeviceMetricsOverride', {width,height,deviceScaleFactor:1,mobile:width<768});
    await page.evaluate(() => scrollTo(0,0));
    const dimensions = await page.evaluate(() => ({width:innerWidth,scroll:document.documentElement.scrollWidth,drawer:document.querySelector('#catalog-drawer').open}));
    assert(dimensions.scroll <= dimensions.width, `Horizontal overflow at ${width}`);
    if (width<768) assert(!dimensions.drawer, 'Mobile directory did not collapse');
    if (width < 768) { await page.click('#new'); await ready(); }
    await page.screenshot({path:root+'/'+name});
    report.push(`Width ${width}: no horizontal overflow`);
  }
  await page.click('#catalog-drawer>summary');
  assert(await page.evaluate(() => document.querySelector('#catalog-drawer').open), 'Mobile directory cannot expand');
  await page.click('#catalog button[data-capability="enterprise_opening"]'); await ready();
  await page.click('#catalog-drawer>summary');
  await page.fill('#question','开户需要什么材料');
  await page.focus('#question'); await page.press('#question','Tab');
  assert(await page.evaluate(() => document.activeElement.id === 'send'), 'Keyboard cannot reach send');
  await page.press('#send','Enter');
  await page.waitForFunction(() => document.querySelector('#messages').textContent.includes('附件未成功读取'));
  await ready();
  await page.evaluate(() => document.querySelector('#messages').scrollIntoView({block:'start'}));
  await page.screenshot({path:root+'/06-手机知识答复.png'});
  report.push('Mobile native directory, knowledge selection and keyboard submit');
  assert((await ask('银行卡被盗刷了')).includes('不能挂失、冻结账户'), 'Urgent action boundary missing');
  assert(await page.evaluate(() => !document.querySelector('#safety').hidden), 'Urgent warning not visible');
  await ask('企业开户材料');
  await page.click('#nav-cancel'); await ready();
  assert(await page.evaluate(() => !document.querySelector('#safety').hidden), 'Ordinary cancel hid urgent warning');
  await page.evaluate(() => scrollTo(0,0));
  await page.screenshot({path:root+'/07-手机紧急提示.png'});
  await page.click('#dismiss-safety');
  await page.waitForFunction(() => document.querySelector('#safety').hidden);
  report.push('Urgent warning survives ordinary cancellation; explicit dismissal');
  await page.click('#new'); await ready();
  await ask('这个怎么办'); await ask('那具体呢');
  assert(await page.evaluate(() => document.querySelector('#consultation-status').textContent.includes('2/2')), 'Clarification count missing');
  await page.click('#nav-cancel'); await ready();
  assert(await page.evaluate(() => document.querySelector('#consultation-status').textContent.includes('已取消')), 'Clarification cannot be cancelled');
  report.push('Two clarification prompts and cancellation');
  await page.evaluate(() => {
    window.financialUiErrors = [];
    addEventListener('error', event => { if (event.message) window.financialUiErrors.push(event.message); });
    addEventListener('unhandledrejection', event => window.financialUiErrors.push(String(event.reason)));
    const original = window.fetch;
    window.financialUiFetch = original;
    window.financialUiRejectNext = true;
    window.financialUiRequestCount = 0;
    window.fetch = (input, init) => {
      if (String(input).endsWith('/messages') && init?.method === 'POST') {
        window.financialUiRequestCount++;
        if (window.financialUiRejectNext) {
          window.financialUiRejectNext = false;
          const body = JSON.parse(init.body); body.text = '测'.repeat(2001);
          init = {...init, body:JSON.stringify(body)};
        }
      }
      return original(input,init);
    };
  });
  await page.fill('#question','企业开户材料'); await page.click('#send');
  await page.waitForFunction(() => document.querySelector('#status').textContent.includes('输入已保留'));
  assert(await page.evaluate(() => document.querySelector('#question').value === '企业开户材料'), 'Failed submit lost input');
  assert(await page.evaluate(() => window.financialUiRequestCount === 1), 'Failed submit duplicated request');
  await page.evaluate(() => { window.fetch=window.financialUiFetch; delete window.financialUiFetch; });
  report.push('Real server 422 preserves original draft; single request');
  await page.evaluate(() => document.querySelector('#advisor-portrait').dispatchEvent(new Event('error')));
  assert(await page.evaluate(() => !document.querySelector('#portrait-fallback').hidden), 'Image fallback unavailable');
  await page.evaluate(() => { document.querySelector('#advisor-portrait').hidden=false;document.querySelector('#portrait-fallback').hidden=true; });
  report.push('Portrait fallback remains text-only and consultation usable');
  assert((await ask('企业开户材料')).includes('附件未成功读取'), 'Service did not recover after failure');
  assert((await page.evaluate(() => window.financialUiErrors)).length === 0, 'Uncaught JavaScript error in verified flow');
  await page.cdp('Emulation.setDeviceMetricsOverride', {width:1440,height:1000,deviceScaleFactor:1,mobile:false});
  await page.evaluate(() => scrollTo(0,0));
  assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), '1440 width overflow');
  report.push('1440 width and successful recovery; no uncaught error in post-refresh flow');
  return report;
}
