// Synthetic audio replaces the physical microphone ONLY inside this QA page.
// MediaRecorder encoding, HTTP upload, local Whisper and final DeepSeek are real.
export async function verify(page, root) {
  const fs=await import('node:fs/promises');
  const output=root+'/docs/evidence/阶段2/2026-10-04-V1.5本机识别修订';
  await fs.mkdir(output,{recursive:true});
  const audio=(await fs.readFile(root+'/data/asr-demo.wav')).toString('base64');
  const assert=(ok,message)=>{if(!ok) throw new Error(message);};
  await page.cdp('Page.reload',{ignoreCache:true});
  await page.waitForFunction(()=>!document.querySelector('#send').disabled && !document.querySelector('#local-voice-start').disabled);
  await page.cdp('Emulation.setDeviceMetricsOverride',{width:1440,height:1100,deviceScaleFactor:1,mobile:false});
  await page.click('#local-voice-start');
  await page.screenshot({path:output+'/01-本机识别数据流.png',fullPage:true});
  await page.click('#local-voice-decline');
  assert(await page.evaluate(()=>document.querySelector('#dynamic-advisor').dataset.state!=='listening'),'Decline started recording');
  await page.evaluate(base64=>{
    window.qaUiErrors=[];
    addEventListener('error',event=>window.qaUiErrors.push(String(event.message)));
    addEventListener('unhandledrejection',event=>window.qaUiErrors.push(String(event.reason)));
    const bytes=Uint8Array.from(atob(base64),c=>c.charCodeAt(0));
    navigator.mediaDevices.getUserMedia=async()=>{
      window.qaSyntheticEnded=false;
      const context=new AudioContext();window.qaAudioContext=context;
      const buffer=await context.decodeAudioData(bytes.buffer.slice(0));
      await context.resume();
      const destination=context.createMediaStreamDestination();
      const source=context.createBufferSource();source.buffer=buffer;source.connect(destination);
      source.onended=()=>{window.qaSyntheticEnded=true;};
      source.start(context.currentTime+0.3);window.qaCapturedStream=destination.stream;
      return destination.stream;
    };
  },audio);
  await page.click('#local-voice-start');await page.click('#local-voice-accept');
  await page.waitForFunction(()=>!document.querySelector('#local-voice-finish').hidden);
  await page.waitForFunction(()=>window.qaSyntheticEnded,undefined,{timeout:15000});
  await page.click('#local-voice-finish');
  await page.waitForFunction(()=>document.querySelector('#dynamic-advisor').dataset.state==='reviewing',undefined,{timeout:60000});
  const recognized=await page.evaluate(()=>({text:document.querySelector('#question').value,
    tracksStopped:window.qaCapturedStream.getTracks().every(track=>track.readyState==='ended'),
    noAnswerYet:document.querySelectorAll('#messages .message').length===0,errors:window.qaUiErrors}));
  assert(recognized.text.includes('网上银行') && recognized.text.includes('收费'),'Actual local model did not transcribe question');
  assert(recognized.tracksStopped && recognized.noAnswerYet && !recognized.errors.length,'Recording safety or deferred send failed');
  await page.screenshot({path:output+'/02-真实本机转写待确认.png',fullPage:true});
  await page.evaluate(()=>window.qaAudioContext.close());
  await page.cdp('Page.reload',{ignoreCache:true});
  await page.waitForFunction(()=>document.querySelector('#dynamic-advisor').dataset.state==='reviewing');
  assert(await page.evaluate(()=>document.querySelector('#question').value.includes('网上银行')),'Refresh lost unconfirmed transcript');
  await page.fill('#question','交通银行香港网上银行服务收费吗？');await page.click('#send');
  await page.waitForFunction(()=>!document.querySelector('#send').disabled && document.querySelector('#messages').textContent.includes('免费'),undefined,{timeout:45000});
  const answered=await page.evaluate(async()=>{
    const sid=active.id;
    const jobs=await api(`/sessions/${sid}/transcription-jobs`);
    const answers=await api(`/sessions/${sid}/answer-jobs`);
    return {sid,processing:answers[0].result.processing,answer:answers[0].result.answer.text,
      jobId:answers[0].id,transcriptionJobs:jobs,revision:active.revision};
  });
  assert(answered.processing==='deepseek','Final answer was not actual DeepSeek');
  await page.screenshot({path:output+'/03-转写确认与真实答复.png',fullPage:true});
  const views=[];
  for(const [width,height] of [[1280,1000],[768,1024],[390,844]]) {
    await page.cdp('Emulation.setDeviceMetricsOverride',{width,height,deviceScaleFactor:1,mobile:width<768});
    assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),'Horizontal overflow: '+width);
    views.push(width);
  }
  await page.screenshot({path:output+'/04-手机宽度本机录音入口.png',fullPage:true});
  await fs.writeFile(output+'/实际浏览器链路.json',JSON.stringify({date:'2026-10-04',
    microphone:'synthetic stream, not a physical microphone test',recorder:'actual browser MediaRecorder webm/opus',
    recognition:'actual local whisper.cpp small; no cloud ASR',recognized,answered,
    refreshedReview:true,viewports:[1440,...views],physicalPhone:'pending',productAcceptance:'deferred'},null,2));
  return {recognized,answered,viewports:[1440,...views]};
}
