"use strict";
(() => {
  const labels = {idle:"AI 顾问 · 等待咨询",listening:"正在聆听",transcribing:"正在本机识别",reviewing:"请核对转写",thinking:"正在查阅资料与组织答复",speaking:"正在语音回复",error:"语音暂不可用 · 文字可继续"};
  let recognizer = null, audioRound = 0, consent = false, last = null, pending = null, lastQuestion = "";
  let enabled = false, utteranceRef = null, localAudio = null, audioUrl = null, nativeRound = -1;
  const state = (name, text) => {
    $("dynamic-advisor").dataset.state = name;
    $("advisor-state").textContent = labels[name];
    $("advisor-wave").hidden = !["listening","speaking","thinking","transcribing"].includes(name);
    if (text) $("voice-status").textContent = text;
  };
  function silence() {
    window.localVoice?.stop();
    audioRound++;
    $("voice-consent").hidden=true;
    if (recognizer) { recognizer.onend = recognizer.onresult = recognizer.onerror = null; recognizer.abort(); recognizer = null; }
    window.speechSynthesis?.cancel();
    if(localAudio) { localAudio.pause(); localAudio.src=""; localAudio=null; }
    if(audioUrl) { URL.revokeObjectURL(audioUrl); audioUrl=null; }
    utteranceRef=null;
    if (enabled) state("idle");
  }
  async function cancel() {
    silence();
    const old = pending; pending = null;
    if (old?.id) {
      try { await api(`/sessions/${old.sid}/answer-jobs/${old.id}/cancel`, {method:"POST"}); }
      catch { $("voice-status").textContent = "已停止展示；服务端取消未确认，请刷新历史核对。"; }
    }
  }
  async function poll(job, sid, version) {
    for (let count=0; count<100; count++) {
      if (version !== epoch || active?.id !== sid) return null;
      if (!["pending","running"].includes(job.status)) return job;
      await new Promise(resolve => setTimeout(resolve, 400));
      job = await api(`/sessions/${sid}/answer-jobs/${job.id}`);
    }
    throw new Error("答复尚未完成，请停止或刷新历史核对。");
  }
  async function send(text, intent="answer") {
    if (!active || waiting || !text.trim()) return;
    silence();
    const sid=active.id, version=++epoch;
    setBusy(true); state("thinking", "知识库优先；需要时查询登记官网页面，再由 DeepSeek 整理。可停止当前回复。");
    $("question").value="";
    try {
      await window.localVoice?.confirm(text);
      if(version!==epoch || active?.id!==sid) return;
      let job = await api(`/sessions/${sid}/answer-jobs`, {method:"POST", body:JSON.stringify({
        text,expected_revision:active.revision || 0,request_id:crypto.randomUUID(),intent})});
      if (version !== epoch || active?.id !== sid) {
        await api(`/sessions/${sid}/answer-jobs/${job.id}/cancel`, {method:"POST"}); return;
      }
      pending={id:job.id,sid}; active.revision=job.revision;
      job = await poll(job,sid,version);
      if (!job || version !== epoch || active?.id !== sid) return;
      pending=null;
      await syncHistory(sid,version);
      if (job.status !== "completed" || !job.result) throw new Error("本轮答复中断或已取消，没有自动重试付费任务。");
      last={sid,id:job.id,text:job.result.answer.text,revision:job.revision}; lastQuestion=text;
      $("voice-replay").disabled=false;
      const names={deepseek:"DeepSeek 根据资料摘录整理",local_fallback:"模型未成功，使用本地资料",no_evidence:"资料不足，未编造答案"};
      const web={retrieved:"已获取本轮官网片段",not_needed:"本地知识优先",unavailable:"官网没有有效补充"};
      state("idle",`${names[job.result.processing]} · ${web[job.result.web_status]}${job.result.draft ? " · 人工辅助草稿，尚未发送" : ""}`);
      setStatus("答复与参考来源已保存");
      if ($("voice-auto").checked && !safety.active) speak(last.text);
    } catch(error) {
      if (version !== epoch || active?.id !== sid) return;
      if (error.status === 422 && intent === "answer") {
        setBusy(false); return sendQuestion(text,null,true);
      }
      if (!$("question").value) $("question").value=text;
      state("idle",error.message); setStatus(error.message);
      try { await syncHistory(sid,version); } catch { /* Keep input for explicit retry. */ }
    } finally { if (version===epoch) { pending=null; setBusy(false); } }
  }
  function speak(text) {
    silence();
    if (!window.speechSynthesis || !window.SpeechSynthesisUtterance) { return localSpeak(audioRound,active?.id); }
    const round=audioRound, sid=active?.id;
    const utterance=new SpeechSynthesisUtterance(text);
    utteranceRef=utterance;
    let started=false;
    utterance.lang="zh-CN"; utterance.rate=1;
    const voice=window.speechSynthesis.getVoices().find(v => /^zh[-_]CN/i.test(v.lang)) || window.speechSynthesis.getVoices().find(v => /^zh/i.test(v.lang));
    if (voice) utterance.voice=voice;
    utterance.onstart=() => { started=true; if(round===audioRound && sid===active?.id) state("speaking","正在播报，可点击停止当前回复。"); };
    utterance.onend=() => { if(round===audioRound && sid===active?.id) { utteranceRef=null; if(started) state("idle","播报结束。可以继续提问。"); else localSpeak(round,sid); } };
    utterance.onerror=() => { if(round===audioRound && sid===active?.id) localSpeak(round,sid); };
    window.speechSynthesis.speak(utterance);
  }
  async function localSpeak(round,sid) {
    if(!last || last.sid!==sid || round!==audioRound || sid!==active?.id || safety.active) return;
    if(nativeRound===round) return;
    nativeRound=round;
    state("thinking","浏览器未开始播报，正在生成本机系统语音；音频仅临时使用。");
    try {
      const response=await fetch(`/api/v1/sessions/${sid}/answer-jobs/${last.id}/speech`,{method:"POST",signal:AbortSignal.timeout(30000)});
      if(!response.ok) throw new Error("当前播放环境无法提供语音，请查看文字。");
      const blob=await response.blob();
      if(round!==audioRound || sid!==active?.id || safety.active) return;
      audioUrl=URL.createObjectURL(blob);localAudio=new Audio(audioUrl);
      localAudio.onplaying=()=>{if(round===audioRound) state("speaking","正在播放本机系统语音，可点击停止当前回复。");};
      localAudio.onended=()=>{if(round===audioRound) state("idle","播报结束。可以继续提问。");};
      localAudio.onerror=()=>{if(round===audioRound) state("error","音频播放未成功，文字仍可查看。");};
      await localAudio.play();
    } catch(error) { if(round===audioRound) state("error",error.message); }
  }
  async function resume(sid, version) {
    if (!enabled) return;
    try {
      const jobs=await api(`/sessions/${sid}/answer-jobs`);
      if(version!==epoch || active?.id!==sid) return;
      const newest=jobs[0];
      if(!newest) return;
      if(newest.status==="completed" && newest.result && newest.revision===active.revision) {
        last={sid,id:newest.id,text:newest.result.answer.text,revision:newest.revision};
        $("voice-replay").disabled=false;
        state("idle","历史答复已恢复；不会自动开启麦克风或播报。点击朗读前重新核对出处。");
      } else if(["pending","running"].includes(newest.status)) {
        pending={sid,id:newest.id}; setBusy(true); state("thinking","正在恢复未完成任务，不会重复调用模型。");
        const job=await poll(newest,sid,version);
        if(version!==epoch || active?.id!==sid) return;
        pending=null; await syncHistory(sid,version); setBusy(false);
        state("idle",job?.status==="completed" ? "答复已恢复并保存，未自动播报。" : "原任务已中断，可明确发起新的咨询。");
      } else if(newest.status==="failed") state("idle","上一任务中断，未自动重新付费请求；可以发起新咨询。");
    } catch { if(version===epoch) {setBusy(false);state("idle","历史任务暂时无法核对，文字记录仍可查看。");} }
    finally { if(version===epoch && active?.id===sid) await window.localVoice?.resume(sid,version); }
  }
  function listen() {
    if (!active || waiting) { $("voice-status").textContent="先选择个人或企业服务，等待或停止当前回复后再开始识别。"; return; }
    const Recognition=window.SpeechRecognition || window.webkitSpeechRecognition;
    if (!Recognition) { state("error","此浏览器不支持语音识别，请用文字咨询；可尝试支持语音识别的浏览器。"); return; }
    silence(); const round=audioRound, sid=active.id;
    recognizer=new Recognition(); recognizer.lang="zh-CN"; recognizer.continuous=false; recognizer.interimResults=true;
    recognizer.onstart=() => { if(round===audioRound && sid===active?.id) state("listening","请说出非敏感公开问题。转写后可修改，再点击发送。"); };
    recognizer.onresult=event => {
      if(round!==audioRound || sid!==active?.id) return;
      const result=event.results[event.results.length-1];
      $("question").value=result[0].transcript.slice(0,1000);
      if(result.isFinal) state("reviewing","转写完成，请核对或修改输入框，再点击发送咨询。");
    };
    recognizer.onerror=event => { if(round===audioRound) state("error",event.error==="network" ? "浏览器无法连接语音识别服务，请点击本机语音提问；文字咨询仍可继续。" : `语音识别未成功（${event.error}），文字咨询仍可继续。`); };
    recognizer.onend=() => { if(round===audioRound && $("dynamic-advisor").dataset.state==="listening") state("reviewing","识别结束，请核对输入；没有结果时可改用文字。"); };
    try { recognizer.start(); } catch { state("error","无法启动识别，请检查浏览器麦克风权限，或使用文字咨询。"); }
  }
  $("voice-start").addEventListener("click",() => { if(consent) listen(); else $("voice-consent").hidden=false; });
  $("voice-accept").addEventListener("click",() => { consent=true; $("voice-consent").hidden=true; listen(); });
  $("voice-decline").addEventListener("click",() => { $("voice-consent").hidden=true; state("idle","继续使用文字咨询，未启动麦克风。"); });
  $("voice-stop").addEventListener("click",async () => {
    const sid=active?.id; const version=++epoch;
    await cancel();
    if (version!==epoch) return;
    setBusy(false); state("idle","当前识别、播报或答复已停止，未自动重播。可继续输入新问题。");
    if(sid) { try { await syncHistory(sid,version); } catch { /* Reported on next refresh. */ } }
  });
  $("voice-replay").addEventListener("click",async () => {
    if (!last || last.sid!==active?.id || last.revision!==active.revision || safety.active) { state("idle","会话已更新，请重新获取适用答复；紧急提示期间不自动播报普通答案。"); return; }
    const round=audioRound, sid=active.id;
    try { const job=await api(`/sessions/${sid}/answer-jobs/${last.id}`);
      if(round===audioRound && sid===active?.id && job.status==="completed") speak(job.result.answer.text);
    } catch { state("error","答复来源无法核对，暂不重播。请继续查看文字。"); }
  });
  $("agent-assist").addEventListener("click",() => {
    const text=$("question").value.trim() || lastQuestion;
    if(!text) { $("voice-status").textContent="请在输入框写需要人工参考的公开问题，再点击准备人工参考。"; return; }
    send(text,"agent_assist");
  });
  window.addEventListener("pagehide",silence);
  window.v15={get enabled(){return enabled;},send,cancel,silence,resume,state};
  api("/capabilities").then(data => { enabled=data.generation; $("voice-panel").hidden=!enabled;
    if(enabled) { state("idle"); document.querySelector(".text-tag").textContent="文字 / 语音演示"; $("mode").textContent="V1.5 · 知识库优先 / 官网定向检索 / DeepSeek 资料答复"; }
  }).catch(() => { /* Earlier running server retains original text service. */ });
})();
