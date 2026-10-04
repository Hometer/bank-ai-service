// Real recorder/Whisper API, synthetic microphone source; no paid model calls.
export async function verify(page, root) {
  const fs=await import('node:fs/promises');
  const output=root+'/docs/evidence/阶段2/2026-10-04-V1.5转写质量优化';
  await fs.mkdir(output,{recursive:true});
  const audio=(await fs.readFile(root+'/data/asr-quality-demo.wav')).toString('base64');
  const assert=(ok,message)=>{if(!ok)throw new Error(message);};
  await page.selectOption('#customer','personal');
  await page.waitForFunction(()=>!document.querySelector('#send').disabled && !document.querySelector('#local-voice-start').disabled);
  await page.fill('#question','上一次有错字的旧转写');
  await page.cdp('Emulation.setDeviceMetricsOverride',{width:1440,height:1100,deviceScaleFactor:1,mobile:false});
  await page.evaluate(base64=>{
    window.qaErrors=[];
    addEventListener('error',event=>window.qaErrors.push(String(event.message)));
    addEventListener('unhandledrejection',event=>window.qaErrors.push(String(event.reason)));
    const bytes=Uint8Array.from(atob(base64),c=>c.charCodeAt(0));
    navigator.mediaDevices.getUserMedia=async()=>{
      window.qaAudioEnded=false;
      const context=new AudioContext();window.qaSyntheticContext=context;
      const buffer=await context.decodeAudioData(bytes.buffer.slice(0));await context.resume();
      const destination=context.createMediaStreamDestination();
      const source=context.createBufferSource();source.buffer=buffer;source.connect(destination);
      source.onended=()=>{window.qaAudioEnded=true;};source.start(context.currentTime+.4);
      window.qaCaptured=destination.stream;return destination.stream;
    };
  },audio);
  await page.click('#local-voice-start');await page.click('#local-voice-accept');
  await page.waitForFunction(()=>document.querySelector('#local-mic-level').value>1,undefined,{timeout:15000});
  const level=await page.evaluate(()=>document.querySelector('#local-mic-level').value);
  await page.screenshot({path:output+'/01-实际音频音量指示.png',fullPage:true});
  await page.waitForFunction(()=>window.qaAudioEnded,undefined,{timeout:15000});
  await page.click('#local-voice-finish');
  await page.waitForFunction(()=>document.querySelector('#dynamic-advisor').dataset.state==='reviewing',undefined,{timeout:60000});
  const recognized=await page.evaluate(()=>({text:document.querySelector('#question').value,preview:document.querySelector('#local-voice-transcript').textContent,
    tracksEnded:window.qaCaptured.getTracks().every(track=>track.readyState==='ended'),errors:window.qaErrors}));
  assert(recognized.text.includes('名下') && recognized.text.includes('余额'),'Financial terms misrecognized');
  assert(recognized.text===recognized.preview,'New recording left stale composer text');
  assert(recognized.tracksEnded && !recognized.errors.length,'Recording resources/error check failed');
  await page.screenshot({path:output+'/02-余额与名下实际转写.png',fullPage:true});
  await page.evaluate(()=>window.qaSyntheticContext.close());
  const viewports=[];
  for(const [width,height] of [[1280,1000],[768,1024],[390,844]]) {
    await page.cdp('Emulation.setDeviceMetricsOverride',{width,height,deviceScaleFactor:1,mobile:width<768});
    assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),'Overflow at '+width);
    viewports.push(width);
  }
  await page.screenshot({path:output+'/03-手机宽度转写确认.png',fullPage:true});
  await page.click('#send');
  await page.waitForFunction(()=>!document.querySelector('#send').disabled && document.querySelector('#messages').textContent.includes('不会展示模拟余额'));
  const privateQueryProtected=await page.evaluate(async()=>{
    const session=await api(`/sessions/${active.id}`), last=session.messages.at(-1);
    return last?.text.includes('查询尚未接入') && last.text.includes('不会展示模拟余额');
  });
  assert(privateQueryProtected,'Voice balance query bypassed private guard');
  const result={date:'2026-10-04',audio:'frozen synthetic Ting-Ting, not real user microphone',
    recorder:'actual browser MediaRecorder opus',model:'actual local large-v3-turbo-q5_0',
    level,recognized,privateQueryProtected,viewports:[1440,...viewports],paidCallsAdded:0};
  await fs.writeFile(output+'/实际浏览器识别核验.json',JSON.stringify(result,null,2));
  return result;
}

export async function verifyFeedbackAndEdits(page, root) {
  const assert=(ok,message)=>{if(!ok)throw new Error(message);};
  await page.cdp('Page.reload',{ignoreCache:true});
  await page.waitForFunction(()=>!document.querySelector('#local-voice-start').disabled);
  await page.evaluate(()=>{
    window.qaAmplitude=.0001;
    navigator.mediaDevices.getUserMedia=async()=>{
      const context=new AudioContext();window.qaToneContext=context;await context.resume();
      const dest=context.createMediaStreamDestination(),source=context.createOscillator(),gain=context.createGain();
      gain.gain.value=window.qaAmplitude;source.connect(gain);gain.connect(dest);source.start();
      return dest.stream;
    };
  });
  await page.click('#local-voice-start');await page.click('#local-voice-accept');
  await page.waitForFunction(()=>document.querySelector('#local-mic-hint').textContent.includes('声音偏小'));
  const quiet=true;
  await page.click('#voice-stop');await page.evaluate(()=>window.qaToneContext.close());
  await page.evaluate(()=>{window.qaAmplitude=1.5;});
  await page.click('#local-voice-start');await page.click('#local-voice-accept');
  await page.waitForFunction(()=>document.querySelector('#local-mic-hint').textContent.includes('声音过大'));
  const clipping=true;
  await page.click('#voice-stop');await page.evaluate(()=>window.qaToneContext.close());
  await page.fill('#question','录音前的原草稿');
  await page.evaluate(()=>{
    window.qaAmplitude=.1;window.qaRealFetch=window.fetch;
    window.fetch=(url,options)=>String(url).endsWith('/transcription-jobs')&&options?.method==='POST'
      ? new Promise(resolve=>{window.qaResolveUpload=()=>resolve(new Response(JSON.stringify({
          id:'mock-only-transcription',status:'completed',revision:active.revision,text:'模拟的新转写',confirmed:false,
        }),{status:202,headers:{'Content-Type':'application/json'}}));})
      : window.qaRealFetch(url,options);
  });
  await page.click('#local-voice-start');await page.click('#local-voice-accept');
  await page.waitForFunction(()=>!document.querySelector('#local-voice-finish').hidden);
  await page.click('#local-voice-finish');await page.waitForFunction(()=>!!window.qaResolveUpload);
  await page.fill('#question','识别期间由用户手动修改的文字');
  await page.evaluate(()=>window.qaResolveUpload());
  await page.waitForFunction(()=>document.querySelector('#dynamic-advisor').dataset.state==='reviewing');
  const editsPreserved=await page.evaluate(()=>document.querySelector('#question').value==='识别期间由用户手动修改的文字');
  assert(editsPreserved,'Recognition overwrote manual edits');
  await page.click('#local-voice-use');
  const explicitUse=await page.evaluate(()=>document.querySelector('#question').value==='模拟的新转写');
  assert(explicitUse,'Explicit use transcript action failed');
  const cleanup=await page.evaluate(()=>document.querySelector('#local-mic-feedback').hidden);
  assert(cleanup,'Meter was left running after recording');
  await page.evaluate(()=>{window.fetch=window.qaRealFetch;return window.qaToneContext.close();});
  await page.cdp('Page.reload',{ignoreCache:true});
  const fs=await import('node:fs/promises');
  const result={kind:'synthetic oscillator input; final transcript response mocked for edit-race QA only',
    quiet,clipping,editsPreserved,explicitUse,cleanup};
  await fs.writeFile(root+'/docs/evidence/阶段2/2026-10-04-V1.5转写质量优化/音量与手动修改核验.json',JSON.stringify(result,null,2));
  return result;
}
