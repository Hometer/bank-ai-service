// Operate the actual local FAQ UI and record actual HTTP results; no model requests.
export async function verify(page, output) {
  const fs = await import('node:fs/promises');
  await fs.mkdir(output, {recursive:true});
  const report = [], samples = [];
  const assert = (value,message) => { if (!value) throw new Error(message); };
  const ready = () => page.waitForFunction(()=>Boolean(document.querySelector('#send')) && !document.querySelector('#send').disabled);
  const install = () => page.evaluate(()=>{
    window.bankFaqReplies = []; window.bankFaqUiErrors = [];
    addEventListener('error', e=>window.bankFaqUiErrors.push(String(e.message)));
    addEventListener('unhandledrejection', e=>window.bankFaqUiErrors.push(String(e.reason)));
    const original=window.bankFaqOriginalFetch || window.fetch;
    window.bankFaqOriginalFetch=original;
    window.fetch=async (...args)=>{
      const response=await original(...args);
      if (String(args[0]).endsWith('/messages') && args[1]?.method==='POST') {
        window.bankFaqReplies.push({question:JSON.parse(args[1].body).text,result:await response.clone().json()});
      }
      return response;
    };
  });
  const ask = async question => {
    await ready(); await page.fill('#question',question); await page.click('#send');
    await page.waitForFunction(q=>window.bankFaqReplies.at(-1)?.question===q && !document.querySelector('#send').disabled,question);
    return page.evaluate(()=>window.bankFaqReplies.at(-1).result);
  };
  const ids = answer => new Set(answer.sources.flatMap(s=>s.article_ids));
  await install();
  assert(await page.evaluate(()=>document.querySelector('#knowledge-status').textContent.includes('146')), 'Knowledge badge not actual count');
  assert(await page.evaluate(()=>document.querySelectorAll('#catalog button').length===7 && !Array.from(document.querySelectorAll('#catalog button')).some(b=>/汇丰|恒生|中银|渣打|东亚/.test(b.textContent))), 'Personal original/new catalog missing');
  await page.screenshot({path:output+'/01-桌面知识目录.png'});
  const cases=[
    ['FQ-01','personal','交银香港网银服务是否免费','HKFAQ-BOCOM-07'],
    ['FQ-02','personal','交银香港转数快是什么','HKFAQ-BOCOM-33'],
    ['FQ-03','personal','交银香港网银密码怎样重置','HKFAQ-BOCOM-29'],
    ['FQ-04','personal','交银香港换新手机要注意什么','HKFAQ-BOCOM-31'],
    ['FQ-05','personal','交银香港FPS支持美元吗','HKFAQ-BOCOM-34'],
    ['FQ-06','personal','交银香港网购认证通过就一定付款成功吗','HKFAQ-BOCOM-36'],
    ['FQ-07','personal','交银香港没收到电子结单提示电邮怎么办','HKFAQ-BOCOM-37'],
    ['FQ-08','enterprise','交银香港企业转数快退款有什么期限','HKFAQ-BOCOM-10'],
    ['FQ-09','enterprise','交银香港企业员工查看电子结单需要什么权限','HKFAQ-BOCOM-39'],
    ['FQ-10','enterprise','交银香港按揭最低利率多少',null],
  ];
  let kind='personal';
  for (const [id,scope,question,expected] of cases) {
    if (scope!==kind) {await page.selectOption('#customer',scope);await ready();kind=scope;}
    const result=await ask(question), answer=result.answer;
    if (expected) assert(ids(answer).has(expected), id+' wrong fact: '+JSON.stringify(answer));
    else assert(answer.kind==='unknown' && !answer.sources.length, 'Unknown rate invented source');
    assert(answer.processing==='knowledge', 'Unexpected model routing');
    samples.push({id,customer_type:scope,question,answer,expected_article_id:expected,overall_score:null,unacceptable_reason:null,execution:'actual_browser_and_local_http'});
    if (id==='FQ-03') {
      await page.click('#messages>article:last-child>.source>details>summary');
      assert(await page.evaluate(()=>document.querySelector('#messages>article:last-child>.source details').open), 'Source disclosure not open');
      const links=await page.evaluate(()=>Array.from(document.querySelectorAll('#messages>article:last-child>.source a')).map(a=>a.href));
      assert(links.length && links.some(url=>new URL(url).hostname==='www.hsbc.com.hk') && links.every(url=>url.startsWith('https://')), 'Actual peer reference missing or insecure');
      await page.screenshot({path:output+'/02-交银密码帮助与参考来源.png'});
    }
    if (id==='FQ-08') await page.screenshot({path:output+'/03-交银企业退款说明.png'});
  }
  await fs.writeFile(output+'/实际样例.json',JSON.stringify({executed_on:'2026-10-03',model_calls_made:0,samples},null,2)+'\n');
  report.push('10 actual browser questions: 9 corresponding official FAQs / 1 unsupported rate, all local knowledge');
  assert(await page.evaluate(()=>document.querySelectorAll('#catalog button').length===8),'Enterprise original/new catalog missing');
  await page.selectOption('#customer','personal');await ready();await page.click('#new');await ready();
  assert(ids((await ask('汇丰转数快有手续费吗')).answer).has('HKFAQ-HSBC-12'),'HSBC initial fee context missing');
  await page.cdp('Page.reload',{ignoreCache:true});await ready();await install();
  assert(ids((await ask('那费用呢')).answer).has('HKFAQ-HSBC-12'),'Real refresh lost bank context');
  const comparison=(await ask('汇丰手机银行PIN忘了，恒生换新手机怎么办')).answer;
  assert(ids(comparison).has('HKFAQ-HSBC-05') && ids(comparison).has('HKFAQ-HASE-07') && ids(comparison).size===2,'Bank clauses mixed');
  await page.screenshot({path:output+'/04-两行规则分别引用.png'});
  report.push('Bank switch, separate bank clauses and real refresh follow-up');
  await page.click('#catalog button[data-capability="personal_faq_bocom"]');await ready();
  await page.click('#nav-next');
  await page.waitForFunction(()=>document.querySelector('#service-cards').textContent.includes('分步阅读 2/'));
  await page.cdp('Page.reload',{ignoreCache:true});await ready();await install();
  assert(await page.evaluate(()=>document.querySelector('#service-cards').textContent.includes('分步阅读 2/')),'FAQ reading step lost after refresh');
  await page.click('#nav-cancel');await ready();
  report.push('New bank FAQ directory, reading step, refresh persistence and cancellation');
  for (const [width,height] of [[1280,900],[768,1024],[390,844]]) {
    await page.cdp('Emulation.setDeviceMetricsOverride',{width,height,deviceScaleFactor:1,mobile:width<768});
    await page.waitForFunction(w=>innerWidth===w && (w>=768 || !document.querySelector('#catalog-drawer').open),width);
    assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),'Horizontal overflow at '+width);
    report.push('Width '+width+': no horizontal overflow');
  }
  assert(await page.evaluate(()=>!document.querySelector('#catalog-drawer').open),'Mobile directory not collapsed');
  await page.click('#catalog-drawer>summary');
  await page.click('#catalog button[data-capability="personal_faq_bocom"]');await ready();
  await page.click('#catalog-drawer>summary');
  assert(ids((await ask('交银香港FPS支持美元吗')).answer).has('HKFAQ-BOCOM-34'),'Mobile bank FAQ missing');
  await page.screenshot({path:output+'/05-手机交银转数快.png'});
  assert((await ask('我的汇丰银行余额')).answer.kind==='private_unavailable','Private protection changed');
  assert((await ask('验证码是123456')).answer.kind==='sensitive_blocked','Credential protection changed');
  assert((await ask('银行卡被盗刷了')).answer.kind==='urgent','Urgent protection changed');
  assert(await page.evaluate(()=>!document.querySelector('#safety').hidden),'Urgent warning invisible');
  await page.evaluate(()=>scrollTo(0,0));
  await page.screenshot({path:output+'/06-手机紧急保护.png'});
  report.push('Mobile bank directory and private/credential/urgent protections');
  assert((await page.evaluate(()=>window.bankFaqUiErrors)).length===0,'Uncaught UI error in verified flow');
  await fs.writeFile(output+'/实际样例.json',JSON.stringify({executed_on:'2026-10-03',model_calls_made:0,samples},null,2)+'\n');
  await fs.writeFile(output+'/浏览器核验.json',JSON.stringify({executed_on:'2026-10-03',checks:report},null,2)+'\n');
  await page.cdp('Emulation.setDeviceMetricsOverride',{width:1440,height:1000,deviceScaleFactor:1,mobile:false});
  return report;
}
