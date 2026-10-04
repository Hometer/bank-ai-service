// Real HTTP/model flows; simulated speech events are explicitly reported separately.
export async function verify(page, output) {
  const fs=await import('node:fs/promises'); await fs.mkdir(output,{recursive:true});
  const checks=[], assert=(ok,message)=>{if(!ok) throw new Error(message);};
  const ready=()=>page.waitForFunction(()=>document.querySelector('#send') && !document.querySelector('#send').disabled);
  await page.cdp('Page.reload',{ignoreCache:true}); await ready();
  await page.evaluate(()=>{window.v15UiErrors=[]; addEventListener('error',e=>window.v15UiErrors.push(String(e.message)));addEventListener('unhandledrejection',e=>window.v15UiErrors.push(String(e.reason)));});
  await page.cdp('Emulation.setDeviceMetricsOverride',{width:1440,height:1100,deviceScaleFactor:1,mobile:false});
  await page.screenshot({path:output+'/01-桌面真实答复与语音控制.png',fullPage:true});
  await page.click('#voice-start'); await page.waitForSelector('#voice-consent',{state:'visible'});
  await page.click('#voice-decline');
  checks.push('Real consent UI: declining does not start microphone');
  // Avoid touching OS/browser-owned microphone prompts during automated QA.
  await page.evaluate(()=>{
    class FakeRecognition {
      start(){window.v15FakeRecognition=this;this.onstart?.();}
      abort(){this.aborted=true;}
    }
    window.SpeechRecognition=window.webkitSpeechRecognition=FakeRecognition;
  });
  await page.click('#voice-start');await page.click('#voice-accept');
  assert(await page.evaluate(()=>document.querySelector('#dynamic-advisor').dataset.state==='listening'),'Listening state missing');
  await page.evaluate(()=>{const result=[{transcript:'交银香港网银服务是否免费'}];result.isFinal=true;window.v15FakeRecognition.onresult({results:[result]});});
  assert(await page.evaluate(()=>document.querySelector('#question').value==='交银香港网银服务是否免费'),'Transcription missing');
  await page.fill('#question','用户修改后的非敏感问题');
  await page.click('#voice-stop');
  assert(await page.evaluate(()=>document.querySelector('#question').value==='用户修改后的非敏感问题'),'Stop destroyed edited text');
  await page.click('#voice-start');
  await page.evaluate(()=>{window.v15LateRecognition=window.v15FakeRecognition.onresult;});
  await page.click('#voice-stop');
  await page.evaluate(()=>{const result=[{transcript:'已取消的迟到识别'}];result.isFinal=true;window.v15LateRecognition({results:[result]});});
  assert(await page.evaluate(()=>!document.querySelector('#question').value.includes('迟到')),'Cancelled transcription returned');
  checks.push('Simulated ASR only: listening/final transcription/edit/stop/late event rejection');
  await page.evaluate(()=>{window.SpeechRecognition=window.webkitSpeechRecognition=undefined;});
  await page.click('#voice-start');
  assert(await page.evaluate(()=>document.querySelector('#voice-status').textContent.includes('不支持语音识别')),'Unsupported recognition not explained');
  checks.push('Unsupported recognition keeps text composer available');
  // Reload removes all simulated speech capabilities before real model checks.
  await page.cdp('Page.reload',{ignoreCache:true});await ready();
  await page.selectOption('#customer','enterprise');await ready();
  await page.fill('#question','交银香港官网转数快退款有什么期限');await page.click('#send');
  await page.waitForFunction(()=>!document.querySelector('#send').disabled && document.querySelector('#messages').textContent.includes('30天'),undefined,{timeout:40000});
  assert(await page.evaluate(()=>document.querySelector('#voice-status').textContent.includes('本轮官网片段')),'No actual fresh webpage');
  await page.screenshot({path:output+'/03-企业官网与DeepSeek实际答复.png',fullPage:true});
  checks.push('Real browser -> real official webpage -> real DeepSeek: refund conditions and sources');
  await page.fill('#question','交银香港转数快退款有什么期限');await page.click('#agent-assist');
  await page.waitForFunction(()=>!document.querySelector('#send').disabled && document.querySelector('#messages').textContent.includes('人工辅助草稿'),undefined,{timeout:40000});
  assert(await page.evaluate(()=>document.querySelector('#messages').textContent.includes('此草稿没有发送给坐席')),'Draft looks like actual handoff');
  await page.screenshot({path:output+'/04-人工辅助真实草稿.png',fullPage:true});
  checks.push('Real DeepSeek draft: source-labelled guidance; not sent to agent');
  for (const [width,height] of [[1280,1000],[768,1024],[390,844]]) {
    await page.cdp('Emulation.setDeviceMetricsOverride',{width,height,deviceScaleFactor:1,mobile:width<768});
    await page.waitForFunction(w=>innerWidth===w,width);
    assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),'Overflow at '+width);
    checks.push('Viewport '+width+': no horizontal overflow');
  }
  await page.screenshot({path:output+'/05-手机语音及人工辅助.png',fullPage:true});
  const before=await page.evaluate(()=>document.querySelectorAll('#messages .message').length);
  await page.fill('#question','验证码是123456');await page.click('#send');
  await page.waitForFunction(()=>!document.querySelector('#send').disabled && document.querySelector('#messages').textContent.includes('敏感输入已屏蔽'));
  assert(await page.evaluate(()=>!document.querySelector('#messages').textContent.includes('123456')),'Sensitive value rendered');
  await page.fill('#question','银行卡被盗刷了');await page.click('#send');
  await page.waitForSelector('#safety',{state:'visible'});await ready();
  assert(await page.evaluate(()=>document.querySelector('#dynamic-advisor').dataset.state!=='speaking'),'Urgent response left old speech');
  checks.push('Real legacy sensitive/urgent routes preserved; urgent silence');
  await page.click('#dismiss-safety');await page.waitForSelector('#safety',{state:'hidden'});
  await page.cdp('Emulation.setDeviceMetricsOverride',{width:1440,height:1100,deviceScaleFactor:1,mobile:false});
  await page.cdp('Page.reload',{ignoreCache:true});await ready();
  assert(await page.evaluate(()=>document.querySelectorAll('#messages .message').length>=4),'History lost after reload');
  assert(await page.evaluate(()=>!speechSynthesis.speaking && document.querySelector('#dynamic-advisor').dataset.state==='idle'),'Refresh started audio');
  checks.push('Actual page refresh restores history, never auto-starts audio');
  await fs.writeFile(output+'/浏览器核验.json',JSON.stringify({date:'2026-10-04',checks,
    asr:'Simulated event verification only; real microphone/cloud recognition pending',
    actual_tts:'Browser previously reported end without start; failure UI verified. Actual audible playback pending',
    full_duplex:false},null,2));
  return checks;
}
