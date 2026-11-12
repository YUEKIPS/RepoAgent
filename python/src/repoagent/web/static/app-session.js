"use strict";
const $ = (id) => document.getElementById(id);
const token = document.querySelector('meta[name="session-token"]').content;
const jobs = new Map();
let configuration, selected, pending, polling = false, switchingSource = false;
let inspectorTrigger;
let activeSession=null, sessionCursor=null, turnCursor=null, serverActive=null, navigating=false, pendingSession=null, creatingSession=false;
const sessions=new Map(), drafts=new Map();
function storageKey(key) {return (configuration?.storage_namespace || 'repoagent-080-unbound')+':'+key;}
function storageGet(key) {try {return JSON.parse(sessionStorage.getItem(storageKey(key)));}catch {return null;}}
function storageSet(key,value) {try {if(value===null) sessionStorage.removeItem(storageKey(key));else sessionStorage.setItem(storageKey(key),JSON.stringify(value));}catch {}}
// Running ownership survives changes to the session currently being read.
const runningTasks=new Map();
let pollTimer=null, inspectedVersion=null, toastTimer=null;
const cardVersions=new WeakMap();
const welcome=$('welcome');
function canBrowse() {return !pending && !pendingSession && !navigating && !switchingSource && !creatingSession;}
function hasRunning() {return !!serverActive || runningTasks.size>0 || [...jobs.values()].some(j=>j.state==='running');}
function frozen() {return !canBrowse() || hasRunning();}
const stream=RepoStream.create({token,isRunning:id=>runningTasks.has(id) || jobs.get(id)?.state==='running',onDraft:(id)=>{
  const job=jobs.get(id),card=document.getElementById(`answer-${id}`);
  if(job?.state==='running' && card) updateRunningCard(card,job);
}});
const liveDrafts=stream.drafts;
function followStream(id) {stream.follow(id);}
function trackTask(job) {
  if(job.state==='running') runningTasks.set(job.id,job);
  else {runningTasks.delete(job.id);stream.finish(job.id);}
}
function announce(text) {if($('stream-status').textContent!==text)$('stream-status').textContent=text;}
function toast(text) {if(toastTimer!==null)clearTimeout(toastTimer);$('toast').textContent=text;$('toast').hidden=false;toastTimer=setTimeout(()=>$('toast').hidden=true,6000);}
function contextText(context) {
  return `历史原文 ${context.included_turn_ids?.length || 0} 轮 · 摘要覆盖 ${context.summarized_turn_ids?.length || 0} 轮${context.uncovered_turns ? ` · 未覆盖 ${context.uncovered_turns} 轮` : ''}`;
}
function renderSessionContext() {
  if(!activeSession)return;
  const latest=[...jobs.values()].at(-1),context=latest?.result?.conversation_context || latest?.context;
  $('session-context').textContent=`${workspaceLabel(activeSession.workspace)} · ${activeSession.turn_count}轮问答${context?' · '+contextText(context):''}${activeSession.organization?.read_only?' · 只读':''}`;
}
function runningText(job) {
  const stages={plan:'规划检索',act:'调查代码',answer:'整理回答',summary:'整理上下文摘要',history_summary:'整理历史摘要',step_summary:'整理步骤摘要'};
  const p=job.progress;
  return p?.step ? `第 ${p.step} 步 · ${stages[p.stage] || '处理结果'}${p.event==='tool_observed'?' · 已取得工具反馈':p.event==='step_started'?' · 正在请求模型':''}。` : '正在检索并分析代码。';
}
function updateRunningCard(card,job) {
  const status=card.querySelector('.answer-label span:last-child');
  if(status)status.textContent=job.cancel_requested?'正在停止…':'分析中…';
  const draft=liveDrafts.get(job.id),text=draft?.text ? '生成中 · 尚未完成引用校验\n\n'+draft.text : runningText(job);
  const body=card.querySelector('.answer-body');if(body.textContent!==text)body.textContent=text;
  announce(job.cancel_requested?'正在停止任务':runningText(job));
}
function cardVersion(job) {
  if(job.state!=='running')return JSON.stringify([job.state,job.question,job.mode,job.simulated,job.workspace,job.context,job.result?.answer,job.result?.execution_status,job.result?.answer_status,job.result?.citations,job.result?.citation_diagnostics,job.result?.affected_claim_ids,job.result?.conversation_context,job.result?.notices,job.artifact_error,job.publication_status,job.error,unpublishedDraft(job),completedWork(job)]);
  return JSON.stringify([job.state,job.question,job.mode,job.simulated,job.workspace,job.context]);
}

const errors = {stale_session_parent:"会话已有新的问答，已刷新记录；请核对后重新发送。",workspace_version_conflict:"会话版本不一致，请重新选择会话。",session_repository_conflict:"问题仓库与会话不一致。",workspace_snapshot_unavailable:"原仓库版本暂不可用，请恢复原快照或新建会话；旧回答仍可查看。",conversation_storage_requires_review:"会话保存异常，请导出记录并检查本地存储后再继续。",interrupted_review_usage_before_retry:"上次任务因服务中断而停止；请核查用量记录，不会自动重发。",model_source_changed:"模型来源已在其他页面切换；已更新连接，请核对后重新发送。",model_source_unavailable:"当前服务未配置此模型来源。",task_already_running:"已有任务运行中，请等待或停止后再提问。",request_id_conflict:"请求编号冲突，未发送新任务。",saved_request_id_already_exists:"该请求已有保存记录，未重复执行。",session_token_required:"会话已变更，请刷新页面。",invalid_request_or_unavailable_source:"输入无效或引用源码暂不可用，请核对快照。"};
class ApiError extends Error { constructor(message,status,code) {super(message);this.status=status;this.code=code;} }
Object.assign(errors,{checkpoint_expired:'原任务期限已过，不能继续；可另开问题。',checkpoint_usage_unknown:'原请求发送或用量尚未核清，不能自动重发。',checkpoint_response_unavailable:'原请求已有用量记录但完整响应未保存，不能自动重新生成。',source_operation_unsettled:'源码操作中断于未结算边界，不能安全自动重复。',checkpoint_configuration_mismatch:'模型或执行配置已变化，不能恢复原任务。',checkpoint_integrity_or_source_unavailable:'检查点或原固定源码无法完整核验，未继续执行。',checkpoint_unavailable:'没有可恢复的完整检查点。',checkpoint_clock_changed:'系统时钟发生明显变化，未延长原任务期限。',task_not_resumable:'此任务已经完成或没有可恢复状态。'});
function node(tag, text, cls) { const n = document.createElement(tag); if (text !== undefined) n.textContent = text; if (cls) n.className = cls; return n; }
async function api(path, body) {
  const res = await fetch(path, {method:body === undefined ? "GET" : "POST", headers:{"X-RepoAgent-Token":token, ...(body === undefined ? {} : {"Content-Type":"application/json"})}, body:body === undefined ? undefined : JSON.stringify(body)});
  const data = await res.json();
  if (!res.ok) throw new ApiError(errors[data.error] || data.error || "请求失败",res.status,data.error);
  return data;
}
function showError(err) {if($("new-session-dialog")?.open){$("new-session-error").textContent=err.message || String(err);return;}if($('run-settings-dialog')?.open){$('settings-error').textContent=err.message || String(err);return;} if($('organization-action')?.open){$('organization-error').textContent=err.message || String(err);return;}if($('repository-manager')?.open){$('repository-status').textContent=err.message || String(err);return;}const target=$("task-inspector").open ? $("inspector-error") : $("error");target.textContent = err.message || String(err); }
function workspaceLabel(workspace) {return workspace?.repositories?.map(r=>`${configuration?.repositories?.find(repo=>repo.id===r.repository)?.name || r.repository} @ ${r.commit.slice(0,12)}`).join(" ＋ ") || "";}
function jobScope(job) {return job.workspace ? workspaceLabel(job.workspace) : `${job.repository} @ ${job.commit.slice(0,12)}`;}
function chosenRepositories() {return LibraryUI.chosen();}
const limitFields = {"limit-recent-turns":"recent_turns","limit-summary-output":"summary_output_tokens","limit-cold-catalog":"cold_catalog_bytes","limit-input":"max_input_estimate","limit-model":"max_model_requests","limit-tools":"max_tool_calls","limit-seconds":"deadline_seconds","limit-output":"max_output_estimate","limit-plan-output":"plan_output_estimate"};
function limitSchema(field) {const schema=configuration.limits_schema.properties[field];return schema?.anyOf ? {...schema.anyOf.find(s=>s.type==="integer" || s.type==="number"),default:schema.default} : schema;}
function syncThinkingOptions() {
  const old=$('native-thinking').value;
  const choices=configuration.thinking_options || ['default'];
  $('native-thinking').replaceChildren(...choices.map(value=>{const option=node('option',value==='default'?'服务默认':value==='off'?'关闭思考':value);option.value=value;return option;}));
  $('native-thinking').value=choices.includes(old)?old:'default';
}
function resetLimits() {
  $("answer-style").value=configuration.policy_default?.answer_style || "auto";
  $("output-mode").value="auto";
  syncThinkingOptions();$("native-thinking").value="default";
  for (const [id,field] of Object.entries(limitFields)) {
    const schema=limitSchema(field), input=$(id);if(!schema) continue;
    input.min=schema.minimum ?? schema.exclusiveMinimum + 0.1;
    const output=['max_output_estimate','plan_output_estimate'].includes(field);
    input.max=output ? (configuration.model_capabilities?.max_output_tokens ?? '') : (schema.maximum ?? ''); input.title=field==='deadline_seconds'?'留空不限时；单次请求等待单独设置':output?'留空采用自动或通用额度':`默认 ${schema.default}；上限 ${schema.maximum}`; input.step=field === "deadline_seconds" ? "any" : "1";
    input.value=schema.default ?? '';
  }
  busy();
  if($('summary-enabled')) $('summary-enabled').checked=true;
  if($('stream-enabled')) $('stream-enabled').checked=true;
}
function taskLimits() {
  const limits={};
  for (const [id,field] of Object.entries(limitFields)) {
    if(field==='max_output_estimate' && $('output-mode').value==='auto') {limits[field]=null;continue;}
    if(field==='plan_output_estimate' && !$(id).value) {limits[field]=null;continue;}
    if(field==='deadline_seconds' && !$(id).value) {limits[field]=null;continue;}
    const value=Number($(id).value), schema=limitSchema(field);if(!schema) continue;
    const maximum=['max_output_estimate','plan_output_estimate'].includes(field)?configuration.model_capabilities?.max_output_tokens:schema.maximum;
    if (!$(id).value || !Number.isFinite(value) || (maximum!=null && value > maximum) || value <= (schema.exclusiveMinimum ?? -Infinity) || value < (schema.minimum ?? -Infinity) || (field !== "deadline_seconds" && !Number.isInteger(value))) throw new Error("请检查运行设置的数值范围。");
    limits[field]=value;
  }
  limits.summary_enabled=$('summary-enabled')?.checked ?? true;
  limits.stream=$('stream-enabled')?.checked ?? true;
  return limits;
}
function busy() {
  const running = hasRunning();
  $("send").disabled = !configuration || !activeSession || activeSession.organization?.read_only || frozen();
  if(configuration)LibraryUI.renderScope();
  $("new-session").disabled = !configuration || running || !!pending || navigating || switchingSource || creatingSession;
  $("new-session").textContent=creatingSession ? "正在创建…" : pendingSession ? "重试新建对话" : "＋ 新建对话";
  $("more-sessions").disabled=!canBrowse();$("older-turns").disabled=!canBrowse();
  document.querySelectorAll("#task-list .session-name").forEach(button=>button.disabled=!canBrowse());
  document.querySelectorAll('[data-write]').forEach(button=>button.disabled=frozen());
  $('running-status').hidden=!running;
  const task=runningTasks.get(serverActive) || [...runningTasks.values()][0];
  $('running-message').textContent=running ? (task?.session_id && task.session_id!==activeSession?.id?'另一对话正在分析，可继续阅读历史。':'正在分析，可以查阅历史或代码引用。') : '';
  $('return-running').hidden=!task?.session_id || task.session_id===activeSession?.id;
  $('return-running').disabled=!canBrowse();
  const sources=configuration?.model_sources || [];
  $("toggle-model-source").disabled = !configuration || sources.length < 2 || running || !!pending || switchingSource;
  $("cancel").hidden = !running;$("cancel").textContent="停止正在运行的任务"; $("retry").hidden = !pending;
  $("mode").disabled = running || !!pending || switchingSource;
  for(const id of [...Object.keys(limitFields),"answer-style","native-thinking","output-mode","reset-limits","summary-enabled","stream-enabled","auto-resume"]) $(id).disabled = !configuration || running || !!pending || switchingSource;
  if($('output-mode').value==='auto') $('limit-output').disabled=true;
  if(configuration)LibraryUI.busy();
}
function currentModelSource() { return configuration.model_source; }
function renderConnection() {
syncThinkingOptions();
$("model-name").textContent=configuration.model;$("connection-badge").textContent=configuration.simulated ? "模拟" : "真实模型";$("budget-label").textContent=configuration.simulated?'预设脚本响应 · 不调用外部模型':'记录实际token · 不计算估算费用';$("notice").textContent=configuration.simulated ? "模拟模式：预设脚本只用于验收，请使用匹配的演示问题；不会理解任意新问题。" : "问题、所需历史与选中源码发送至当前模型，费用由配置的API账户承担。";
  $("notice").className=configuration.simulated ? "notice simulated-notice" : "notice";
  $("toggle-model-source").textContent=configuration.simulated ? "切换到真实模型" : "切换到模拟模式";
}
async function switchModelSource() {
  if(!configuration || switchingSource || pending || hasRunning() || (configuration.model_sources || []).length < 2) return;
  const target=currentModelSource() === "live" ? "mock" : "live";
  switchingSource=true;busy();$("error").textContent="";
  try {
    configuration=await api("/api/model-source",{model_source:target});renderConnection();
  } catch(error) {
    showError(error);
    // 切换响应丢失时先读服务器真实状态，绝不盲目翻转或提交问题。
    try { configuration=await api("/api/config");renderConnection(); } catch(checkError) { showError(checkError); }
  } finally {switchingSource=false;busy();}
}
function noticeItems(result) { return result?.notices || []; }
function noticeGroup(parent,title,items,open=false) {
  if(!items.length) return;
  const details=node("details",undefined,"notice-group");details.open=open;
  details.append(node("summary",`${title} · ${items.length}项`));
  for(const item of items) details.append(node("p",item.text,"scope-note"));
  details.addEventListener("click",event=>event.stopPropagation());parent.append(details);
}
function taskChanged(job,summary) {
  return !job || JSON.stringify(job)!==JSON.stringify(summary);
}
function openInspector(job, trigger) {
  selected=job.id;inspect(job);inspectorTrigger=trigger || document.activeElement;
  const dialog=$("task-inspector");
  if(!dialog.open) dialog.showModal();
  $("open-inspector").setAttribute("aria-expanded","true");
  document.querySelectorAll(".answer-card").forEach(card=>card.classList.remove("selected"));
  document.getElementById(`answer-${job.id}`)?.classList.add("selected");

}
function selectDebugTab(button) {
  const buttons=[...document.querySelectorAll("[data-tab]")];
  buttons.forEach(item=>{item.setAttribute("aria-selected",String(item===button));item.tabIndex=item===button ? 0 : -1;});
  for(const tab of ["trace","usage","raw"]) $(tab).hidden=tab!==button.dataset.tab;
}
const textErrorLabels={unsupported_text_encoding:"文本编码不支持",text_encoding_required:"需要指定文本编码",text_encoding_conflict:"指定编码与文件标记冲突",unsupported_text_characters:"文件包含不支持的控制字符",decoded_text_limit:"解码文本超过容量限制"};
Object.assign(errors,textErrorLabels,{invalid_text_profile:"编码配置无效，请核对文件和编码。",encoding_file_required:"请选择文件及文本编码。",file_outside_readable_catalog:"该文件不在当前版本的可读取范围内。"});
function fileReadIssue(job) {
  const notice=job.result?.notices?.find(n=>n.source==='program' && textErrorLabels[n.code]);
  if(notice)return notice.text;
  return '';
}
function failureText(job) {
  const result=job.result, code=result?.stop_reason || job.error?.code;
  const notice=result?.notices?.find(n=>n.source==='program' && n.code==='execution_stopped');
  if(notice) return notice.text;
  if(code==='cancelled') return '已按你的要求停止，不会自动继续。';
  if(code==='task_start_failed') return '任务未能启动。请查看任务详情，确认服务状态后再手动提交。';
  if(errors[code]) return errors[code];
  if(code?.startsWith('model_http_') || code==='request_timeout') return '模型服务未能完成本次请求。请求可能已发送，请先核查用量，再决定是否重新提交。';
  return fileReadIssue(job) || '本次分析未能完成，未发布正式答案。请查看任务详情；重新提交前请核查已有用量。';
}
function unpublishedDraft(job) {
  if(job.publication_status==='uncommitted' && typeof job.uncommitted_result?.answer==='string') return job.uncommitted_result.answer;
  if(job.result?.execution_status==='SUCCEEDED' || job.result?.answer) return '';
  const responses=(job.result?.trace || []).filter(e=>e.event==='model_response' && e.response?.kind==='answer');
  const answer=responses.at(-1)?.response?.answer;
  return ['answered','insufficient_evidence'].includes(answer?.answer_status) && typeof answer.answer==='string' ? answer.answer : '';
}
function completedWork(job) {
  if(!job.result) return '本轮没有可用的完整执行记录。';
  const reads=(job.result.trace || []).filter(e=>e.event==='evidence_added' && e.evidence);
  const files=new Set(reads.map(e=>JSON.stringify([e.evidence.repo_id,e.evidence.path])));
  return files.size ? `本轮已取得 ${files.size} 个文件的部分源码证据；这不代表问题已完成分析。` : '本轮未取得可用于回答的源码证据。';
}
function renderJobs() {
  const target=$('messages'),scroll=target.scrollTop || 0;
  const atBottom=target.scrollHeight-target.clientHeight-scroll<60;
  const wanted=[];
  $("open-inspector").disabled=!jobs.size;
  if(!jobs.size) {
    wanted.push(welcome);
    if(!welcome.isConnected)target.append(welcome);
    $('welcome-title').textContent=activeSession?'可以开始提问了':'从一个问题，读懂这份仓库';
    $('welcome-description').textContent=activeSession?'直接提问或附加日志，回答会附带可核查的代码引用。':'准备仓库的固定读取版本，再新建对话选择分析范围。回答附带代码引用，方便核查。';
    $('welcome-start').hidden=!!activeSession;
    $('welcome-start').disabled=frozen() || !LibraryUI.available().length;
    $('welcome-add').disabled=frozen();
    $('welcome-add').textContent=LibraryUI.available().length?'打开仓库池':'添加仓库';
    $('welcome-start').textContent='新建对话';
  }
  for (const job of jobs.values()) {
    trackTask(job);
    const old=document.getElementById(`answer-${job.id}`),version=cardVersion(job);
    if(old && cardVersions.get(old)===version) {
      old.classList.toggle('selected',selected===job.id);
      if(job.state==='running'){updateRunningCard(old,job);followStream(job.id);}
      wanted.push(old.previousElementSibling,old);continue;
    }
    const user=node('div',job.question,'user-message');
    const card = node("article", undefined, "answer-card" + (selected === job.id ? " selected" : ""));
    cardVersions.set(card,version);
    card.id=`answer-${job.id}`;
    const result = job.result, label = node("div", undefined, "answer-label");
    const stateLabels={SUCCEEDED:"已完成",FAILED:"执行失败",CANCELLED:"已停止",BUDGET_EXCEEDED:"达到执行限制"};
    label.append(node("span", `RepoAgent${job.simulated ? " · 模拟" : ""}${job.mode==='baseline' ? " · Baseline" : ""}`), node("span", job.state === "running" ? (job.cancel_requested ? "正在停止…" : "分析中…") : result?.citation_diagnostics?.length ? '已完成 · 引用待核对' : stateLabels[result?.execution_status] || "执行失败"));
    const progressText=runningText(job);
    const text=result?.answer || (job.state === "running" ? progressText : failureText(job));
    const body=node("div",undefined,"answer-body");
    if(result?.answer && globalThis.RepoMarkdown) body.append(RepoMarkdown.render(text,result));else body.textContent=text;
    card.append(label,body);
    if(result?.citation_diagnostics?.length) {
      const warning=node('div',undefined,'limitation');
      warning.append(node('p','正文已保留，部分引用未通过核对；这不代表相关结论已获证实。'));
      for(const claim of result.claims || []) if((result.affected_claim_ids || []).includes(claim.claim_id)) warning.append(node('p',`[${claim.claim_id}] ${(result.unvalidated_claim_ids || []).includes(claim.claim_id)?'缺少已核对引用':'部分引用有问题'} · ${claim.text}`));
      card.append(warning);
    }
    if(job.state==='running') {
      const draft=liveDrafts.get(job.id);if(draft?.text) body.textContent='生成中 · 尚未完成引用校验\n\n'+draft.text;
      updateRunningCard(card,job);followStream(job.id);
    }
    if(job.state==='interrupted') {
      const resume=node('button','继续上次任务');resume.type='button';resume.disabled=!!serverActive;
      resume.addEventListener('click',async()=>{try{const updated=await api(`/api/tasks/${job.id}/resume`,{});jobs.set(job.id,updated);trackTask(updated);serverActive=job.id;renderJobs();busy();}catch(error){showError(error);}});
      card.append(resume,node('p','仅恢复原预算和期限内、发送状态明确的检查点。','context-note'));
    }
    if(job.state!=='running' && !result?.answer) {
      card.append(node('p',completedWork(job),'context-note'));
      const draft=unpublishedDraft(job);
      if(draft) {
        const details=node('details',undefined,'unpublished-draft');
        details.append(node('summary','查看未发布草稿'),node('p','该草稿未通过完整发布校验，不能作为已核验答案。','limitation'),node('pre',draft));
        card.append(details);
      }
    }
    const context=result?.conversation_context || job.context;
    if(context) card.append(node("p",contextText(context)+" · 历史回答须经本轮源码核查",context.uncovered_turns ? "context-note uncovered-warning" : "context-note"));
    if (result?.answer_status === "insufficient_evidence") card.append(node("p", "证据不足：不能据此认定仓库中不存在相关功能。", "limitation"));
    const notices=noticeItems(result);
    for(const item of notices.filter(n=>n.source==='program' && n.code==='retrieval_finalized')) card.append(node('p',item.text,'limitation'));
    for(const item of notices.filter(n=>n.category === "evidence")) card.append(node("p",`证据与读取限制：${item.text}`,"limitation"));
    noticeGroup(card,"回答范围与待核实（模型说明）",notices.filter(n=>n.category === "answer"),result?.answer_status === "insufficient_evidence");
    if (job.artifact_error) card.append(node("p", "服务端保存失败，请立即导出 JSON 保留记录。", "limitation"));
    const foot=node("div",undefined,"answer-foot"), citationCount=result?.citations?.length || 0;
    const view=node("button",citationCount ? `查看引用 · ${citationCount}处 ↗` : "查看任务详情 ↗");
    view.type="button";view.setAttribute("aria-haspopup","dialog");view.setAttribute("aria-controls","task-inspector");
    view.addEventListener("click",()=>openInspector(jobs.get(job.id) || job,view));
    foot.append(view,node("span",job.workspace ? job.workspace.repositories.map(r=>r.repository).join(" ＋ ") : job.repository));card.append(foot);
    card.setAttribute("aria-label", `回答：${job.question}`);
    wanted.push(user,card);
  }
  for(const child of [...target.children])if(!wanted.includes(child))child.remove();
  wanted.forEach((child,i)=>{if(target.children[i]!==child)target.insertBefore(child,target.children[i] || null);});
  if(atBottom)target.scrollTop=target.scrollHeight;else target.scrollTop=scroll;
  renderSessionContext();busy();
}
const inspector=RepoInspector.create({api,node,showError,jobScope,noticeItems,noticeGroup});
function inspect(job) {
  const version=[job.id,job.state,job.cancel_requested,job.result,job.error,job.artifact_saved,job.artifact_error];
  if(inspectedVersion && version.every((value,i)=>value===inspectedVersion[i]))return;
  inspectedVersion=version;inspector.render(job);
}

function renderSessions() {return LibraryUI.renderSessions();}
async function loadSessions(more=false) {return LibraryUI.loadSessions(more);}
function saveDraft() {if(activeSession) drafts.set(activeSession.id,{question:$("question").value,log:$("log-text").value});}
async function loadSession(identifier, more=false, ownedNavigation=false) {
  if(!ownedNavigation && !canBrowse()) return;
  navigating=true;busy();
  try {
    if(!more) {saveDraft();$('task-inspector').close();inspectedVersion=null;}
    const page=await api(`/api/sessions/${identifier}/turns${more && turnCursor ? `?before=${turnCursor}` : ""}`);
    activeSession=page.session;turnCursor=page.next_before;if(configuration){LibraryUI.onSession(activeSession);await LibraryUI.refreshPool();}
    const older=more?[...jobs.values()]:[];jobs.clear();for(const job of [...page.items,...older]) jobs.set(job.id,job);
    for(const job of jobs.values())trackTask(job);
    if(!more) {const draft=drafts.get(identifier);$("question").value=draft?.question || "";$("log-text").value=draft?.log || "";}
    $("session-title").textContent=activeSession.title;
    renderSessionContext();
    $("older-turns").hidden=!turnCursor;selected=more && jobs.has(selected)?selected:[...jobs.keys()].at(-1);$("open-inspector").disabled=!selected;
    storageSet("selected-session",identifier);renderJobs();renderSessions();if(selected) inspect(jobs.get(selected));
  } finally {navigating=false;busy();}
}
async function newSession() {
  if(creatingSession || pending || hasRunning() || navigating)return;
  if(!pendingSession && !chosenRepositories().length)return showError(new Error('请至少选择一个可用读取版本。'));
  creatingSession=true;saveDraft();
  const carriedDraft={question:$('question').value,log:$('log-text').value};
  pendingSession=pendingSession || {request_id:crypto.randomUUID().replaceAll('-',''),repositories:chosenRepositories()};
  storageSet('new-session-request',pendingSession);busy();
  try {
    const created=await api('/api/sessions',pendingSession);
    await loadSessions();await loadSession(created.id,false,true);
    pendingSession=null;storageSet('new-session-request',null);LibraryUI.created();
    $('question').value=carriedDraft.question;$('log-text').value=carriedDraft.log;$('question').focus();
  } catch(error){
    if(error instanceof ApiError && [400,403,404,409,422].includes(error.status)){pendingSession=null;storageSet('new-session-request',null);}
    showError(error);
  } finally{creatingSession=false;busy();}
}

function pollDelay() {return document.hidden ? 30000 : hasRunning() ? 1500 : 10000;}
function scheduleRefresh() {if(pollTimer!==null)clearTimeout(pollTimer);pollTimer=setTimeout(refresh,pollDelay());}
async function refresh() {
  if(polling)return;
  if(pollTimer!==null){clearTimeout(pollTimer);pollTimer=null;}
  polling=true;
  try {
    const config=await api('/api/config');serverActive=config.active_task;
    if(!pending && !switchingSource && config.model_source!==configuration.model_source){configuration=config;renderConnection();}
    const ids=new Set([...runningTasks.keys(),...[...jobs.values()].filter(j=>j.state==='running').map(j=>j.id)]);
    if(serverActive)ids.add(serverActive);
    let changed=false,selectedChanged=false;
    for(const id of ids) {
      const job=await api(`/api/tasks/${id}`);trackTask(job);
      if(job.session_id===activeSession?.id && taskChanged(jobs.get(job.id),job)) {
        jobs.set(job.id,job);changed=true;selectedChanged ||= selected===job.id;
      }
    }
    if(changed)renderJobs();
    if(selectedChanged && jobs.has(selected))inspect(jobs.get(selected));
    busy();
  } catch(error){showError(error);}finally{polling=false;scheduleRefresh();}
}
document.addEventListener('visibilitychange',()=>{if(!document.hidden)refresh();else scheduleRefresh();});
async function reviewNewSession() {return LibraryUI.openNew();}

async function sendPending() {
  if(!pending) return;
  const submitted=pending;
  $("error").textContent=""; $("retry").disabled=true;
  try { const job=await api("/api/tasks",pending); jobs.set(job.id,job);trackTask(job); selected=job.id; activeSession.head=job.id;activeSession.turn_count=job.sequence;serverActive=job.state==="running"?job.id:null; pending=null;storageSet("pending-turn",null); if($("question").value===submitted.question) $("question").value="";if($("log-text").value===(submitted.log_text || "")) $("log-text").value="";drafts.delete(activeSession.id);renderJobs();inspect(job);await loadSessions();activeSession.title=sessions.get(activeSession.id)?.title || activeSession.title;$("session-title").textContent=activeSession.title;renderSessionContext();$("messages").scrollTop=$("messages").scrollHeight; }
  catch(e) {
    if(e instanceof ApiError && [400,403,404,409,413,415].includes(e.status)) {
      pending=null;storageSet("pending-turn",null);showError(new Error(`${e.message}。本次提交已被拒绝，输入已保留，可修改后重新发送。`));
      if(e.code === "stale_session_parent") {try {await loadSession(activeSession.id);}catch(error){showError(error);}}
      if(e.code === "model_source_changed") {try {configuration=await api("/api/config");renderConnection();} catch(checkError) {showError(checkError);}}
    } else showError(new Error(`${e.message}。结果尚不确定，请保留同一请求编号重试连接。`));
  }
  finally {$("retry").disabled=false;busy();}
}
$("chat-form").addEventListener("submit", e => { e.preventDefault(); if($("send").disabled) return; if(!$("question").value.trim()) return showError(new Error("请填写非空问题。")); let limits;try {limits=taskLimits();}catch(error){$('run-settings-dialog').showModal();return showError(error);}pending={session_id:activeSession.id,workspace_version:activeSession.workspace.version,parent_turn_id:activeSession.head,limits,policy:{...(configuration.policy_default || {}),answer_style:$("answer-style").value,native_thinking:$("native-thinking").value},model_source:currentModelSource(),request_id:crypto.randomUUID().replaceAll("-",""),repository:activeSession.workspace.repositories[0].repository,mode:$("mode").value,question:$("question").value,log_text:$("log-text").value || null};storageSet("pending-turn",pending); busy();sendPending(); });
$("chat-form").addEventListener("invalid",event=>{const details=event.target.closest("details");if(details) details.open=true;},true);
$("output-mode").addEventListener("change",()=>{if($("output-mode").value==='auto') $('limit-output').value='';busy();});
$("question").addEventListener("keydown",e => {if(e.key === "Enter" && (e.ctrlKey || e.metaKey)) {e.preventDefault();$("chat-form").requestSubmit();}});
$("retry").addEventListener("click",sendPending);
$("reset-limits").addEventListener("click",resetLimits);
if($('auto-resume')) $('auto-resume').addEventListener('change',async()=>{if(frozen()){$('auto-resume').checked=configuration.auto_resume;return;} $('auto-resume').disabled=true;try{const result=await api('/api/runtime-settings',{auto_resume:$('auto-resume').checked});configuration.auto_resume=result.auto_resume;toast('服务设置已保存');}catch(error){$('auto-resume').checked=configuration.auto_resume;showError(error);}finally{busy();}});
$("cancel").addEventListener("click",async () => {const identifier=serverActive || [...runningTasks.keys()][0] || [...jobs.values()].find(j=>j.state === "running")?.id; if(!identifier) return;try {const job=await api(`/api/tasks/${identifier}/cancel`,{});trackTask(job);if(job.session_id===activeSession?.id) {jobs.set(job.id,job);renderJobs();}busy();}catch(e){showError(e);}});
$("export").addEventListener("click",() => {if(!selected)return;const blob=new Blob([JSON.stringify(jobs.get(selected),null,2)],{type:"application/json"});const url=URL.createObjectURL(blob),a=node("a");a.href=url;a.download=`repoagent-${selected}.json`;a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);});
document.querySelectorAll("[data-tab]").forEach(button => {
  button.addEventListener("click",()=>selectDebugTab(button));
  button.addEventListener("keydown",event=>{
    const buttons=[...document.querySelectorAll("[data-tab]")], index=buttons.indexOf(button);
    const target=event.key==='ArrowRight' ? buttons[(index+1)%buttons.length] : event.key==='ArrowLeft' ? buttons[(index+buttons.length-1)%buttons.length] : event.key==='Home' ? buttons[0] : event.key==='End' ? buttons.at(-1) : null;
    if(target) {event.preventDefault();selectDebugTab(target);target.focus();}
  });
});
$("open-inspector").addEventListener("click",()=>{if(selected && jobs.has(selected)) openInspector(jobs.get(selected),$("open-inspector"));});
$("close-inspector").addEventListener("click",()=>$("task-inspector").close());
$("task-inspector").addEventListener("close",()=>{$("open-inspector").setAttribute("aria-expanded","false");if(inspectorTrigger?.isConnected) inspectorTrigger.focus();else $("open-inspector").focus();});
$("task-inspector").addEventListener("click",event=>{
  if(event.target!==$("task-inspector")) return;
  const box=event.target.getBoundingClientRect();
  if(event.clientX<box.left || event.clientX>box.right || event.clientY<box.top || event.clientY>box.bottom) event.target.close();
});
$('welcome-add').addEventListener('click',openRepositories);
$('welcome-start').addEventListener('click',reviewNewSession);
$('sidebar-repositories').addEventListener('click',openRepositories);
$('pool-close-icon').addEventListener('click',()=>LibraryUI.closePool());
$('local-import-info').addEventListener('click',()=>$('local-import-note').hidden=false);
$('sidebar-settings').addEventListener('click',()=>{$('settings-error').textContent='';$('run-settings-dialog').showModal();});
$('theme-select').value=RepoTheme.current();$('theme-select').addEventListener('change',()=>RepoTheme.apply($('theme-select').value));
$('open-settings').addEventListener('click',()=>{$('settings-error').textContent='';$('run-settings-dialog').showModal();});
$('close-settings').addEventListener('click',()=>$('run-settings-dialog').close());
$('run-settings-dialog').addEventListener('close',()=>$('open-settings').focus());
$('cancel-new-session').addEventListener('click',()=>LibraryUI.cancelNew());
$('confirm-new-session').addEventListener('click',newSession);
$('return-running').addEventListener('click',async()=>{const task=runningTasks.get(serverActive) || [...runningTasks.values()][0];if(task?.session_id && canBrowse())await loadSession(task.session_id);});
$('dismiss-toast').addEventListener('click',()=>$('toast').hidden=true);
$("example").addEventListener("click",()=>{$("question").value="请介绍当前仓库的主要结构和入口，并给出代码位置。";$("question").focus();});
$("new-session").addEventListener("click",reviewNewSession);
$("more-sessions").addEventListener("click",()=>loadSessions(true).catch(showError));
$("older-turns").addEventListener("click",()=>loadSession(activeSession.id,true).catch(showError));
$("toggle-model-source").addEventListener("click",switchModelSource);
// 移动布局保留历史入口，但首次进入时收起，给正文和输入区留空间。
const narrowLayout=globalThis.matchMedia?.("(max-width:650px)");
const foldMobileHistory=event=>{const history=document.querySelector(".history-section");if(configuration){history?.setAttribute('open','');return;}if(event.matches) history?.removeAttribute("open");else history?.setAttribute("open","");};
if(narrowLayout) {foldMobileHistory(narrowLayout);narrowLayout.addEventListener("change",foldMobileHistory);}
let repositoryJob=null;
Object.assign(errors,{
  github_connect_timeout:"连接GitHub超时，请稍后重新添加仓库。",
  github_tls_timeout:"与GitHub建立加密连接超时，请检查网络后重新添加。",
  github_headers_timeout:"等待GitHub响应超时，请稍后重新添加仓库。",
  github_read_timeout:"读取GitHub数据超时，本次准备未完成，请稍后重新添加仓库。",
  github_connection_failed:"GitHub连接中断，本次获取未完成，请检查网络。",
  github_certificate_failed:"GitHub证书校验失败，请核对本机时间、证书及网络环境。",
  github_tls_failed:"GitHub加密连接失败，请检查网络环境。",
  github_incomplete_response:"GitHub响应未完整接收，请稍后重新添加仓库。",
  dns_timeout:"解析GitHub地址超时，请检查网络。",
  response_too_large:"GitHub返回的数据超过本次读取上限。"
});
Object.assign(errors,{invalid_repository_url:"请填写公开GitHub仓库首页地址，不包含文件路径、凭据或查询参数。",invalid_repository_ref:"分支或提交号格式无效。",public_visibility_not_confirmed:"无法确认这是公开仓库，未使用令牌提升访问权限。",resource_not_visible:"GitHub未返回此资源，请检查地址、版本或公开状态。",rate_limited:"GitHub请求额度受限，请稍后重试；已有缓存仍可使用。",credential_rejected:"GitHub令牌无效或已失效，请在本机更新配置后重启加载。",access_denied:"GitHub拒绝了请求，请核对公开状态与令牌权限。",repository_identity_mismatch:"仓库身份发生变化，已停止获取。",connection_or_tls_failed:"无法安全连接GitHub，请检查网络。",dns_address_rejected:"GitHub连接地址未通过检查。",redirect_requires_review:"仓库地址发生跳转，请核对当前公开地址后重新添加。",source_version_in_use:"此版本仍被会话、任务或引用使用，无法移除。",cache_full_with_protected_content:"缓存已满，现有内容受历史引用保护；无法继续获取新文件。",repository_disk_limit:"仓库存储已达到空间保护上限。",tree_identity_or_completeness_failed:"目录不完整或版本不一致，未登记此版本。",network_request_limit:"本轮仓库请求已达上限，请依据已有证据核查。",network_time_limit:"本轮仓库读取已达时间上限。",catalog_policy_limit:"仓库规模超过当前支持范围，未登记此版本。",cursor_stale:"内容覆盖已变化，请从第一页重新检索。"});
function repositoryName(repo) {return repo.name ? `${repo.name} · ${repo.commit.slice(0,8)}${repo.text_profile ? ' · 文本配置 '+repo.source_version_id.slice(0,6) : ''}` : repo.id;}
function renderRepositoryManager() {return LibraryUI.renderRepositories();}
function openRepositories() {renderRepositoryManager();if(!$('repository-manager').open)$('repository-manager').showModal();if(repositoryJob)pollRepositoryJob();}
async function loadRepositoryDirectory(version,path,target,cursor=0,more=false) {
  try {
    const page=await api(`/api/repositories/${version}/directory?path=${encodeURIComponent(path)}&cursor=${cursor}`);
    if(!more) {target.replaceChildren(node('p',path || '/'));if(path){const up=node('button','上一级');up.type='button';up.addEventListener('click',()=>loadRepositoryDirectory(version,path.split('/').slice(0,-1).join('/'),target));target.append(up);}}
    for(const item of page.items) {
      const line=node('div');line.className='directory-entry';
      if(item.type==='directory'){const button=node('button',item.name+'/');button.type='button';button.addEventListener('click',()=>loadRepositoryDirectory(version,(path?path+'/':'')+item.name,target));line.append(button);}
      else {const state=item.policy!=='allowed'?'已排除':item.fetch_state==='failed'?(textErrorLabels[item.fetch_error] || '获取失败'):({parsed:'已解析',text_only:'文本覆盖'}[item.status] || {cached:'已缓存',fetching:'获取中',reclaimed:'已回收'}[item.fetch_state] || '未获取');line.append(node('span',item.name),node('small',state));
        if(item.policy==='allowed'){
          const details=node('details'),summary=node('summary','文本编码'),select=node('select'),preview=node('button','预览所选编码');details.append(summary);select.setAttribute('aria-label',`${item.name} 文本编码`);
          const current=configuration.repositories.find(r=>r.source_version_id===version)?.text_profile?.overrides[(path?path+'/':'')+item.name] || 'auto';
          for(const [value,label] of [['auto','自动 · UTF-8 / BOM'],['utf-8','UTF-8'],['utf-16','UTF-16 · 需要BOM'],['gbk','GBK'],['gb18030','GB18030']]){const option=node('option',label);option.value=value;option.selected=value===current;select.append(option);}
          const full=(path?path+'/':'')+item.name;preview.type='button';preview.disabled=!!repositoryJob;
          preview.addEventListener('click',()=>startRepositoryJob({action:'preview_encoding',source_version_id:version,path:full,encoding:select.value}));details.append(select,preview);line.append(details);
        }
      }
      target.append(line);
    }
    if(page.next_cursor!=null){const next=node('button','更多文件');next.type='button';next.addEventListener('click',()=>{next.remove();loadRepositoryDirectory(version,path,target,page.next_cursor,true);});target.append(next);}
  } catch(error){target.replaceChildren(node('p',errors[error.code] || '目录暂不可用，请稍后重试。'));}
}
async function reloadRepositories() {
  const config=await api('/api/config');configuration=config;if(configuration)await LibraryUI.refreshPool();renderRepositoryManager();renderConnection();busy();
}
async function startRepositoryJob(command) {
  if(repositoryJob || frozen()) return;
  repositoryJob={...command,request_id:crypto.randomUUID().replaceAll('-','')};storageSet('repository-job',repositoryJob);renderRepositoryManager();$('repository-status').textContent='正在准备，请稍候…';
  try {await api('/api/repositories',repositoryJob);await pollRepositoryJob();}catch(error){$('repository-status').textContent=error.message;if(error instanceof ApiError && error.status<500){repositoryJob=null;storageSet('repository-job',null);renderRepositoryManager();}}
}
async function pollRepositoryJob() {
  if(!repositoryJob)return;
  try {
    const job=await api(`/api/repositories/jobs/${repositoryJob.request_id}`);
    if(['queued','running'].includes(job.state)) {$('repository-status').textContent=job.state==='queued'?'等待仓库读取通道…':'正在核对公开版本与目录…';setTimeout(pollRepositoryJob,1000);return;}
    const result=job.result || {};repositoryJob=null;storageSet('repository-job',null);await reloadRepositories();
    $('repository-status').textContent=result.error?(errors[result.error] || '仓库处理失败，请核查地址与网络后重试。'):job.state==='interrupted'?'上次准备因服务中断停止，未自动重发。':job.state==='cancelled'?'已停止准备。':result.changed===false?'当前分支没有新版本。':result.changed?'发现新版本，建立后可用于新会话；旧会话保持原版本。':result.removed?'已移除未使用版本。':'目录已准备好，请选择仓库并新建会话。当前对话保持原版本。';
    if(result.state==='encoding_preview'){
      const pre=node('pre',result.text),apply=node('button','确认编码，建立新读取版本');pre.className='encoding-preview';apply.type='button';
      $('repository-status').textContent=`${result.path} · 按 ${result.selected_encoding} 预览${result.truncated?'（已截断）':''}。请核对中文；预览不代表已确定原编码。`;
      apply.addEventListener('click',()=>startRepositoryJob({action:'apply_encoding',source_version_id:result.source_version_id,path:result.path,encoding:result.encoding}));$('repository-status').append(pre,apply);
    }
    if(['encoding_ready','catalog_ready'].includes(result.state) && result.repository)LibraryUI.repositoryReady(result);
    if(result.changed) {const button=node('button',`建立新版本 ${result.new_commit.slice(0,12)}`);button.type='button';button.addEventListener('click',()=>startRepositoryJob({action:'update',source_version_id:result.source_version_id,commit:result.new_commit}));$('repository-status').append(button);}
  } catch(error) {$('repository-status').textContent='连接暂时中断，请重新打开仓库管理查看原任务；不会自动重复准备。';}
}
$('close-repositories').addEventListener('click',()=>LibraryUI.closePool());
$('retry-repository-job').addEventListener('click',async()=>{if(!repositoryJob || frozen())return;try{await api('/api/repositories',repositoryJob);await pollRepositoryJob();}catch(error){showError(error);}});
$('repository-form').addEventListener('submit',event=>{event.preventDefault();startRepositoryJob({action:'add',url:$('repository-url').value.trim(),ref:$('repository-ref').value.trim()});});
$('cancel-repository-job').addEventListener('click',async()=>{if(repositoryJob){try{await api(`/api/repositories/jobs/${repositoryJob.request_id}/cancel`,{});}catch(error){$('repository-status').textContent=error.message;}}});
(async () => {try {configuration=await api("/api/config");if($("auto-resume")) $("auto-resume").checked=configuration.auto_resume;await LibraryUI.init({
  get configuration(){return configuration;},get activeSession(){return activeSession;},set activeSession(value){activeSession=value;},
  jobs,sessions,api,node,ApiError,errors,frozen,canBrowse,saveDraft,renderJobs,loadSession,newSession,reviewNewSession,showError,busy,toast,storageGet,storageSet,openRepositories,startRepositoryJob,loadRepositoryDirectory,
  pendingCreation:()=>!!pendingSession,get repositoryJob(){return repositoryJob;},get navigating(){return navigating;},set navigating(value){navigating=value;}
});resetLimits();renderRepositoryManager();renderConnection();serverActive=configuration.active_task;repositoryJob=storageGet('repository-job');if(repositoryJob)pollRepositoryJob();
  pending=storageGet("pending-turn");pendingSession=storageGet("new-session-request");await loadSessions();
  const remembered=pending?.session_id || storageGet("selected-session") || sessions.keys().next().value;
  if(remembered) {try {await loadSession(remembered,false,true);}catch(error){showError(error);}}
  if(pending) {$("question").value=pending.question;$("log-text").value=pending.log_text || "";$("error").textContent="上次提交结果尚未确认，请用原请求编号重试连接；不会自动重发。";}
  renderJobs();busy();refresh();}catch(e){showError(e);$("notice").textContent="无法连接本地服务，请确认启动命令与端口后刷新。";}})();
