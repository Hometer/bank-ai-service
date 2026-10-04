"use strict";
(() => {
  let enabled=false, recorder=null, stream=null, chunks=[], timer=null, generation=0;
  let pending=null, reviewed=null, recordingContext=null;
  let meterContext=null, meterTimer=null;
  const setState=(name,text) => window.v15?.state(name,text);
  const endTracks=() => {
    clearInterval(meterTimer); meterTimer=null;
    meterContext?.close().catch(()=>{}); meterContext=null;
    $("local-mic-feedback").hidden=true;
    stream?.getTracks().forEach(track=>track.stop()); stream=null;
  };
  function monitor(captured,round,sid,revision) {
    if(!window.AudioContext) return;
    try {
      meterContext=new AudioContext();
      const source=meterContext.createMediaStreamSource(captured), analyser=meterContext.createAnalyser();
      analyser.fftSize=2048; source.connect(analyser); // Never connect microphone to speakers.
      const samples=new Float32Array(analyser.fftSize);
      let quiet=0;
      $("local-mic-feedback").hidden=false;
      meterContext.resume().catch(()=>{});
      meterTimer=setInterval(()=>{
        if(!valid(round,sid,revision)) return;
        analyser.getFloatTimeDomainData(samples);
        const rms=Math.sqrt(samples.reduce((sum,x)=>sum+x*x,0)/samples.length);
        const peak=samples.reduce((max,x)=>Math.max(max,Math.abs(x)),0);
        $("local-mic-level").value=Math.min(100,rms*500);
        quiet=rms<0.006 ? quiet+1 : 0;
        $("local-mic-hint").textContent=peak>0.98 ? "声音过大，请稍微远离麦克风。" : quiet>=8 ? "声音偏小或尚未说话，请靠近麦克风，并检查当前输入设备。" : "请用自然语速说完整句子，录音结束前保留一句话的尾音。";
      },250);
    } catch {
      meterContext?.close().catch(()=>{}); meterContext=null;
      // Meter is optional; recording can continue.
    }
  }
  function stop() {
    generation++;
    clearTimeout(timer); timer=null;
    $("local-voice-consent").hidden=true;
    $("local-voice-finish").hidden=true;
    $("local-voice-review").hidden=true;
    if(recorder) { recorder.onstop=null; recorder.ondataavailable=null; if(recorder.state!=="inactive") recorder.stop(); }
    recorder=null; chunks=[]; endTracks();
    recordingContext=null;
    const old=pending; pending=null;
    if(old) api(`/sessions/${old.sid}/transcription-jobs/${old.id}/cancel`,{method:"POST"}).catch(()=>{});
  }
  const valid=(round,sid,revision) => round===generation && active?.id===sid && active.revision===revision;
  const errors={NO_SPEECH:"没有检测到清晰语音，请靠近麦克风重新录音。",ASR_TIMEOUT:"本机识别超时，请缩短录音后重试。",AUDIO_DURATION:"录音过短或超过20秒，请重新录音。",SENSITIVE_TRANSCRIPT:"识别到敏感信息，转写没有保存；请只说公开演示问题。",RESTART_INTERRUPTED:"服务重启中断了识别，请重新录音。"};
  async function waitForJob(job,sid,revision,round,originalDraft=null) {
    for(let i=0;i<145;i++) {
      if(!valid(round,sid,revision)) return;
      if(!["pending","running"].includes(job.status)) {
        pending=null;
        if(job.status==="completed" && job.text) {
          reviewed={sid,id:job.id,revision};
          const copied=showReview(job.text,originalDraft);
          setState("reviewing",copied ? "本机转写完成。请核对或修改输入框，再点击发送咨询；尚未发送给AI。" : "本机转写完成，输入框在识别期间有修改，已保留。可点击使用这段转写，再核对发送。");
        } else setState("idle",errors[job.error_code] || "本机识别已中断，文字咨询仍可继续。");
        return;
      }
      await new Promise(resolve=>setTimeout(resolve,400));
      if(!valid(round,sid,revision)) return;
      job=await api(`/sessions/${sid}/transcription-jobs/${job.id}`);
    }
    stop(); setState("idle","识别等待超时，已取消任务；请重新录音或继续文字咨询。");
  }
  async function submit(blob,sid,revision,round,originalDraft) {
    if(!valid(round,sid,revision)) return;
    setState("transcribing","录音已结束，正在这台Mac上转写；录音不会发送给DeepSeek。可点击停止取消。");
    // Receive the accepted job ID even after a user stops, then cancel it explicitly.
    // Aborting a completed upload would lose the ID and leave an orphan worker.
    const controller=new AbortController(), uploadTimer=setTimeout(()=>controller.abort(),10000);
    try {
      const response=await fetch(`/api/v1/sessions/${sid}/transcription-jobs`,{
        method:"POST",headers:{"Content-Type":blob.type,"X-Expected-Revision":String(revision)},
        body:blob,signal:controller.signal,
      });
      const job=await response.json();
      if(!response.ok) throw new Error(job.error?.message || "录音转写暂不可用，请继续文字咨询。");
      if(!valid(round,sid,revision)) {
        await api(`/sessions/${sid}/transcription-jobs/${job.id}/cancel`,{method:"POST"}); return;
      }
      pending={sid,id:job.id};
      await waitForJob(job,sid,revision,round,originalDraft);
    } catch(error) { if(valid(round,sid,revision)) { stop(); setState("error",error.message); } }
    finally { clearTimeout(uploadTimer); }
  }
  function finishRecording() {
    if(recorder?.state==="recording") { clearTimeout(timer); timer=null; recorder.stop(); endTracks(); $("local-voice-finish").hidden=true; }
  }
  async function start() {
    if(!enabled || !active || waiting) { setState("idle","请选择会话，等待或停止当前答复后再录音。"); return; }
    window.v15.silence();
    const round=generation, sid=active.id, revision=active.revision, originalDraft=$("question").value;
    if(!navigator.mediaDevices?.getUserMedia || !window.MediaRecorder) { setState("error","浏览器不支持本机录音，请使用文字咨询。"); return; }
    setState("listening","请允许浏览器使用麦克风；仅说非敏感公开问题。");
    try {
      const captured=await navigator.mediaDevices.getUserMedia({audio:{echoCancellation:true,noiseSuppression:true,autoGainControl:true,channelCount:{ideal:1}},video:false});
      if(!valid(round,sid,revision)) { captured.getTracks().forEach(track=>track.stop()); return; }
      stream=captured;
      monitor(captured,round,sid,revision);
      const mime=["audio/webm;codecs=opus","audio/mp4","audio/ogg;codecs=opus"].find(type=>MediaRecorder.isTypeSupported(type));
      if(!mime) throw new Error("浏览器没有可用录音格式，请使用文字咨询。");
      recorder=new MediaRecorder(stream,{mimeType:mime,audioBitsPerSecond:128000}); chunks=[]; recordingContext={sid,revision,round};
      recorder.ondataavailable=event=>{ if(valid(round,sid,revision) && event.data.size) chunks.push(event.data); };
      recorder.onerror=()=>{ if(valid(round,sid,revision)) { stop(); setState("error","麦克风录音中断，请重新尝试或使用文字咨询。"); } };
      recorder.onstop=()=>{
        if(!valid(round,sid,revision)) return;
        const blob=new Blob(chunks,{type:mime}); chunks=[]; recorder=null; recordingContext=null; endTracks();
        submit(blob,sid,revision,round,originalDraft);
      };
      recorder.start(); $("local-voice-finish").hidden=false;
      setState("listening","正在本机录音，最长20秒。说完点击“结束录音并转写”；停止当前回复会取消录音。");
      timer=setTimeout(finishRecording,19500);
    } catch(error) {
      if(valid(round,sid,revision)) { stop(); setState("error",error.name==="NotAllowedError" ? "麦克风权限未获允许。请在地址栏的站点设置中允许麦克风，再重新录音。" : error.message); }
    }
  }
  async function confirm(text) {
    if(!reviewed || reviewed.sid!==active?.id || reviewed.revision!==active.revision) return;
    await api(`/sessions/${reviewed.sid}/transcription-jobs/${reviewed.id}/confirm`,{method:"POST",body:JSON.stringify({text})});
    reviewed=null;
  }
  async function resume(sid,version) {
    if(!enabled) return;
    const round=generation;
    try {
      const jobs=await api(`/sessions/${sid}/transcription-jobs`);
      if(active?.id!==sid || version!==epoch || round!==generation || !jobs.length) return;
      const job=jobs[0];
      if(job.status==="completed" && !job.confirmed) {
        reviewed={sid,id:job.id,revision:job.revision};
        showReview(job.text);
        setState("reviewing","本机转写待确认内容已恢复，没有自动发送或开启麦克风。请核对后点击发送。");
      } else if(["pending","running"].includes(job.status)) {
        pending={sid,id:job.id}; setState("transcribing","正在恢复本机识别任务，没有重新录音。");
        await waitForJob(job,sid,job.revision,generation);
      }
    } catch { /* Text consultation remains usable. */ }
  }
  function showReview(text,originalDraft=null) {
    $("local-voice-transcript").textContent=text;
    $("local-voice-review").hidden=false;
    if(!$("question").value.trim() || (originalDraft!==null && $("question").value===originalDraft)) {
      $("question").value=text; return true;
    }
    return false;
  }
  $("local-voice-use").addEventListener("click",()=>{
    $("question").value=$("local-voice-transcript").textContent; $("question").focus();
  });
  $("local-voice-start").addEventListener("click",()=>{
    if(!active || waiting || recordingContext || pending) { setState("idle","请先选择会话或停止当前录音、识别及答复。"); return; }
    $("local-voice-consent").hidden=false;
  });
  $("local-voice-accept").addEventListener("click",start);
  $("local-voice-decline").addEventListener("click",()=>{ $("local-voice-consent").hidden=true; });
  $("local-voice-finish").addEventListener("click",finishRecording);
  window.addEventListener("pagehide",stop);
  window.localVoice={stop,confirm,resume};
  api("/capabilities").then(data=>{
    enabled=data.local_asr; $("local-voice-start").disabled=!enabled;
    $("local-voice-start").title=enabled ? "录音只交本机离线识别" : "本机语音模型尚未准备好，文字可继续";
    if(enabled && active) resume(active.id,epoch);
  }).catch(()=>{});
})();
