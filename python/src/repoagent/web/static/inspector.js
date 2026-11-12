"use strict";
globalThis.RepoInspector={create({api,node,showError,jobScope,noticeItems,noticeGroup}) {
const $=id=>document.getElementById(id);
let sourceVersion=null;
function render(job) {
  $("inspection-empty").hidden = true; $("inspection").hidden = false; $("export").disabled = false;
  $("inspector-error").textContent="";
  $("open-inspector").disabled=false;
  $("inspection-context").textContent=`${jobScope(job)} · ${job.result?.citations?.length || 0}处引用`;
  $("raw").replaceChildren(node("pre",job.result?.answer || "当前任务尚未发布回答。"));
  $("task-meta").replaceChildren();
  const contexts=(job.result?.trace || []).filter(e=>e.event==='request_context');
  const latestContext=contexts.at(-1), rebuilds=(job.result?.trace || []).filter(e=>e.event==='context_rebuilt');
  if(latestContext) {
    const tokens=latestContext.measurement?.estimated_input_tokens;
    $("task-meta").append(node("dt","上下文"),node("dd",`${tokens == null ? '未知' : tokens+' Token（估算）'} · 原文 ${latestContext.hot_evidence_count ?? latestContext.evidence_ranges?.length ?? 0}段 · 冷证据 ${latestContext.cold_evidence_count ?? 0}段`));
    $("task-meta").append(node("dt","最近压缩"),node("dd",rebuilds.length ? (rebuilds.at(-1).reason==='request_pressure' ? '完整输入接近容量限制' : '主动压缩') : '未触发'));
  }
  for (const [key,value] of [["任务", job.id],["仓库版本", job.workspace ? workspaceLabel(job.workspace) : `${job.repository} @ ${job.commit}`],["执行模式",job.mode],["模型来源",job.simulated ? "模拟" : "真实模型"],["任务状态", job.result ? `${job.result.execution_status} / ${job.result.answer_status || "无回答"}` : job.state],["停止原因", job.result?.stop_reason || job.error?.code || "等待完成"],["记录保存", job.artifact_saved ? "已保存" : job.state === "running" ? "执行中" : "请导出保留"]]) $("task-meta").append(node("dt",key),node("dd",value));
  const nextSourceVersion=JSON.stringify([job.id,job.result?.citations,job.result?.citation_contexts,job.result?.citation_diagnostics,job.result?.claims]);
  if(nextSourceVersion!==sourceVersion) {
  sourceVersion=nextSourceVersion;
  $("sources").replaceChildren();
  (job.result?.citations || []).forEach((citation, index) => {
    const box = node("div", undefined, "source-card"), button = node("button", `${citation.evidence_id} · ${citation.repo_id ? citation.repo_id+" / " : ""}${citation.path}:${citation.start_line}–${citation.end_line}`), caption = node("div", `结论 ${citation.claim_ids.join(", ")} · ${citation.commit}${citation.snapshot_id ? " · 快照 "+citation.snapshot_id : ""}`, "source-caption");
    const context=job.result.citation_contexts?.[index];
    box.append(node("p",`精确引用：${citation.start_line}–${citation.end_line}${context ? `；可展开已读上下文：${context.start_line}–${context.end_line}` : ''}`,"source-caption"));
    const expand=node('button','展开已读上下文');expand.type='button';expand.hidden=!context;
    const code = node("pre"); code.hidden = true; const next = node("button", "继续读取"); next.hidden=true; let cursor;
    let contextMode=false;
    async function read() {
      $("inspector-error").textContent="";
      button.disabled=true; next.disabled=true;
      try { const params=[];if(cursor)params.push(`line=${cursor.start_line}`,`column=${cursor.start_column}`);if(contextMode)params.push('context=1');const query=params.length?'?'+params.join('&'):''; const source = await api(`/api/tasks/${job.id}/source/${index}${query}`);
        if (!cursor) code.textContent = "";
        const lines=source.text.split("\n"); if(lines.at(-1)==="") lines.pop();
        code.textContent += lines.map((line,offset)=>`${source.start_line+offset}${offset===0 && source.start_column ? ":"+source.start_column : ""} │ ${line}`).join("\n")+"\n";
        code.hidden=false; cursor=source.continuation; next.hidden=!cursor;
      } catch(e) {showError(e);} finally {button.disabled=false;next.disabled=false;}
    }
    button.addEventListener("click", () => {cursor=null;contextMode=false;read();});expand.addEventListener('click',()=>{cursor=null;contextMode=true;read();}); next.addEventListener("click", read); box.append(button, caption, expand, code, next); $("sources").append(box);
  });
  if (!job.result?.citations?.length) $("sources").append(node("p", "当前没有已发布引用。失败或证据不足不等同于正确拒答。"));
  for (const issue of job.result?.citation_diagnostics || []) {
    const box=node('div',undefined,'source-card');box.append(node('p',`待核对 · ${issue.citation_index == null ? '结论关联' : '原引用 '+(issue.citation_index+1)} · ${(issue.claim_ids || []).join(', ')}`),node('pre',JSON.stringify(issue,null,2)));$('sources').append(box);
  }
  for (const claim of job.result?.claims || []) $("sources").append(node("p", `${claim.claim_id} · ${(job.result.unvalidated_claim_ids || []).includes(claim.claim_id)?'未验证 · ':''}${claim.text}`));
  }
  $("trace").replaceChildren(node("p", "以下为执行完成后的记录。模型请求、工具动作及返回内容属于待分析数据。"));
  const notices=noticeItems(job.result);
  noticeGroup($("trace"),"检索与执行信息",notices.filter(n=>n.category === "execution"),true);
  noticeGroup($("trace"),"验收说明",notices.filter(n=>n.category === "evaluation"),true);
  if(job.limits) $("trace").append(node("pre",JSON.stringify({effective_limits:job.limits,policy:job.policy},null,2)));
  (job.result?.trace || []).forEach((entry,index) => {const details=node("details"); details.append(node("summary",`${String(index+1).padStart(2,"0")} · ${entry.event}${entry.name ? " / "+entry.name : ""}`),node("pre",JSON.stringify(entry,null,2))); $("trace").append(details);});
  const displayed=JSON.parse(JSON.stringify(job.result?.usage || job.error || {status:'等待任务完成'}),undefined);
  $("usage").replaceChildren(node("p", job.simulated ? "模拟任务未调用外部模型。" : "统计服务返回的实际token；推理为输出明细，不重复相加。缺失统计为未知，不代表零消耗。"),node("pre", JSON.stringify(displayed,null,2)));
}
return {render};
}};
