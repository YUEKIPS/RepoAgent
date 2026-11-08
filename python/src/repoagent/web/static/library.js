"use strict";
// Global pool and flat history. Selection belongs only to an unsubmitted new dialog.
globalThis.LibraryUI=(()=>{
  const $=id=>document.getElementById(id),uid=()=>crypto.randomUUID().replaceAll('-','');
  const key=r=>r.source_kind==='github_lazy'?`github:${r.repository_id}`:`local:${r.id}`;
  let host,pool=[],selection=[],sessionCursor=null,command=null,actionTarget=null;
  let returnToNew=false,creatingDraft=false,libraryState='archived',libraryCursor=null;
  const button=(label,action,cls)=>{const b=host.node('button',label,cls);b.type='button';b.addEventListener('click',()=>Promise.resolve().then(action).catch(host.showError));return b;};
  const query=(kind,extra={})=>'/api/organization?'+new URLSearchParams({kind,...extra});
  const display=r=>pool.find(p=>p.repository_key===key(r))?.title || r.name || r.id;
  const versionLabel=r=>`${r.commit.slice(0,12)}${r.text_profile?' · 读取 '+r.source_version_id.slice(0,6):''}`;
  function available(){const keys=new Set(pool.map(p=>p.repository_key));return host.configuration.repositories.filter(r=>keys.has(key(r)) && ['READY','PARTIAL'].includes(r.state));}
  async function refreshPool(){
    const all=[];let before;
    do{const page=await host.api(query('repository',{state:'active',limit:50,...(before?{before}:{})}));all.push(...page.items);before=page.next_before;}while(before);
    pool=all;renderScope();
  }
  function renderScope(){
    const members=host.activeSession?.workspace.repositories || [];
    $('scope-region').hidden=!members.length;
    $('scope-chips').replaceChildren(...members.map(m=>{
      const r=host.configuration.repositories.find(repo=>repo.id===m.repository),chip=host.node('span',undefined,'scope-chip');
      chip.append(host.node('span',r?display(r):m.repository),host.node('code',m.commit.slice(0,12)));chip.title=`${m.repository} @ ${m.commit}`;return chip;
    }));
    $('scope-note').textContent=host.activeSession?.organization?.read_only?'本对话固定仓库 · 历史只读':'本对话固定仓库';
  }
  function chosen(){return selection.slice();}
  function setSelection(ids){selection=[...new Set(ids)];renderSelection();}
  function renderSelection(){
    const box=$('new-repo-options');box.replaceChildren();
    for(const r of available()){
      const row=host.node('label',undefined,'repo-row'+(selection.includes(r.id)?' checked':''));
      const check=host.node('input');check.type='checkbox';check.value=r.id;check.checked=selection.includes(r.id);check.disabled=host.frozen();
      const label=host.node('div');label.append(host.node('strong',display(r)),host.node('small',`${r.source_kind==='github_lazy'?'GitHub':'已登记快照'} · ${versionLabel(r)}`));
      row.append(check,label,host.node('small',r.state==='PARTIAL'?'部分覆盖':'就绪'));
      check.addEventListener('change',()=>{
        if(host.frozen())return;
        if(check.checked && selection.length>=8){check.checked=false;$('new-session-error').textContent='一个对话最多选择8个读取版本。';return;}
        selection=check.checked?[...selection,r.id]:selection.filter(id=>id!==r.id);
        row.classList.toggle('checked',check.checked);$('new-session-error').textContent='';busy();
      });box.append(row);
    }
    if(!box.children.length)box.append(host.node('p','仓库池中暂无可用版本。请先添加公开仓库，准备完成后返回选择。','context-note'));
    $('new-selected-count').textContent=`已选 ${selection.length} / 8 个固定读取版本`;busy();
  }
  async function openNew(){
    if(host.pendingCreation())return host.newSession();
    if(host.frozen())return;
    await refreshPool();
    if(!creatingDraft){selection=[];creatingDraft=true;}
    $('new-session-error').textContent='';renderSelection();$('new-session-dialog').showModal();
  }
  function cancelNew(){creatingDraft=false;selection=[];returnToNew=false;$('new-session-dialog').close();}
  function created(){creatingDraft=false;selection=[];returnToNew=false;$('new-session-dialog').close();}
  function addForNew(){returnToNew=true;$('new-session-dialog').close();host.openRepositories();showPoolTab('add');}
  function showPoolTab(tab){for(const b of document.querySelectorAll('[data-pool-tab]'))b.setAttribute('aria-selected',String(b.dataset.poolTab===tab));$('pool-existing').hidden=tab!=='list';$('pool-add').hidden=tab!=='add';}
  function closePool(){
    $('repository-manager').close();
    if(returnToNew){returnToNew=false;renderSelection();$('new-session-dialog').showModal();}
  }
  function repositoryReady(result){
    if(returnToNew && result.repository && available().some(r=>r.id===result.repository)){
      if(!selection.includes(result.repository) && selection.length<8)selection.push(result.repository);
      renderSelection();
      $('repository-status').append(host.node('p','仓库已准备好。返回新建对话继续确认选择。'));
    }
    busy();
  }
  async function loadSessions(more=false){
    const page=await host.api(query('session',{state:'active',...(more&&sessionCursor?{before:sessionCursor}:{})}));
    if(!more)host.sessions.clear();for(const s of page.items)host.sessions.set(s.id,s);sessionCursor=page.next_before;renderSessions();
  }
  function renderSessions(){
    const nav=$('task-list');nav.replaceChildren();$('task-count').textContent=String(host.sessions.size);
    for(const s of host.sessions.values()){
      const row=host.node('div',undefined,'session-row');
      const b=button('',async()=>{if(host.canBrowse()){await host.loadSession(s.id);toggleSidebar(false);}},'session-name');
      b.append(host.node('span',s.title,'session-name-title'),host.node('small',s.workspace?.repositories.map(m=>host.configuration.repositories.find(r=>r.id===m.repository)?.name || m.repository).join(' ＋ ') || `${s.turn_count || 0}轮问答`));
      b.title=s.title;b.setAttribute('aria-current',String(s.id===host.activeSession?.id));
      row.append(b,button('⋯',()=>menu({...s,kind:'session'}),'item-menu'));nav.append(row);
    }
    if(!nav.children.length)nav.append(host.node('p','还没有对话','context-note'));
    $('more-sessions').hidden=!sessionCursor;host.busy();
  }
  function onSession(s){if(s.organization)s.title=s.organization.title || s.title;renderScope();}
  function menu(item){
    if(host.frozen())return;
    actionTarget=null;const target=$('organization-menu');target.replaceChildren(host.node('h3',item.title || '仓库'));
    const add=(label,action)=>target.append(button(label,()=>{
      if(host.frozen())return;
      if(action==='rename'){
        actionTarget={kind:item.kind,id:item.id,revision:item.revision,action};
        target.replaceChildren(host.node('h3','重命名'));$('organization-fields').hidden=false;$('organization-save').hidden=false;$('organization-title').value=item.title || '';return $('organization-title').focus();
      }
      const c={request_id:uid(),kind:item.kind,action,id:item.id,revision:item.revision};
      if(['archive','trash'].includes(action)){
        target.replaceChildren(host.node('h3',action==='archive'?'确认归档？':'确认移入回收站？'),host.node('p','内容将从活跃列表隐藏，可恢复；历史回答、固定版本和引用保留。'));
        target.append(button('确认',()=>dispatch(c),'confirm-management'),button('返回',()=>menu(item)));
      }else return dispatch(c);
    }));
    add('重命名','rename');
    if(item.state==='trashed')add('恢复','restore');else{add(item.state==='archived'?'取消归档':'归档',item.state==='archived'?'unarchive':'archive');add('移入回收站','trash');}
    $('organization-fields').hidden=true;$('organization-save').hidden=true;
    if(!$('organization-action').open)$('organization-action').showModal();
  }
  async function dispatch(c){
    if(host.frozen())return host.showError(new Error('提交或任务执行期间不能整理内容。'));
    if(command && command.request_id!==c.request_id)return host.showError(new Error('上次管理操作尚未确认，请先重试原操作。'));
    command=c;host.storageSet('management-command',c);$('organization-error').textContent='';$('retry-management').hidden=false;
    try{
      await host.api('/api/organization',c);command=null;host.storageSet('management-command',null);$('retry-management').hidden=true;$('organization-action').close();
      await refreshPool();await loadSessions();renderRepositories();
      if(host.activeSession){const s=await host.api(`/api/sessions/${host.activeSession.id}/turns?limit=1`);host.activeSession.organization=s.session.organization;host.activeSession.title=s.session.organization.title || s.session.title;$('session-title').textContent=host.activeSession.title;}
      if($('organization-library').open)await libraryPage();renderScope();host.busy();host.toast('整理已保存，可从归档或回收站恢复。');return true;
    }catch(e){if(e instanceof host.ApiError && e.status<500){command=null;host.storageSet('management-command',null);$('retry-management').hidden=true;}host.showError(e);return false;}
  }
  async function openLibrary(state){libraryState=state;$('library-heading').textContent=state==='archived'?'归档':'回收站';$('organization-library').showModal();await libraryPage();}
  async function libraryPage(more=false){
    try{
      const kind=$('library-kind').value,page=await host.api(query(kind,{state:libraryState,...(more&&libraryCursor?{before:libraryCursor}:{})}));
      const target=$('library-list');if(!more)target.replaceChildren();
      for(const item of page.items){const row=host.node('div',undefined,'library-row');row.append(host.node('span',item.title || item.repository_key));if(kind==='session')row.append(button('查看历史',async()=>{if(!host.canBrowse())return;await host.loadSession(item.id);$('organization-library').close();}));row.append(button('操作',()=>menu({...item,kind})));target.append(row);}
      if(!target.children.length)target.append(host.node('p','暂无记录。'));libraryCursor=page.next_before;$('more-library').hidden=!libraryCursor;
    }catch(e){host.showError(e);}
  }
  function renderRepositories(){
    $('github-auth-status').textContent=host.configuration.github_auth==='anonymous'?'GitHub匿名连接':'GitHub认证状态：'+host.configuration.github_auth;
    $('repository-cache-status').textContent='仓库池管理固定读取版本；已有对话及历史引用保持原版本。';
    const list=$('repository-list');list.replaceChildren();
    for(const entry of pool){
      const versions=host.configuration.repositories.filter(r=>key(r)===entry.repository_key),repo=versions.at(-1);if(!repo)continue;
      const row=host.node('section',undefined,'repository-item'),head=host.node('div',undefined,'repository-head');head.append(host.node('h3',entry.title || repo.name || repo.id),button('⋯',()=>menu({...entry,kind:'repository'}),'item-menu'));row.append(head);
      for(const version of versions){
        const block=host.node('div',undefined,'version-block');block.append(host.node('p',`${versionLabel(version)} · ${version.state==='PARTIAL'?'部分覆盖':version.state==='READY'?'就绪':version.state}`));
        if(version.source_kind==='github_lazy'){
          const check=button('检查更新',()=>host.startRepositoryJob({action:'check',source_version_id:version.source_version_id}));check.setAttribute('data-write','');block.append(check);
          const tree=host.node('div');tree.hidden=true;block.append(button('浏览目录',()=>{tree.hidden=!tree.hidden;if(!tree.hidden)return host.loadRepositoryDirectory(version.source_version_id,'',tree);}),tree);
        }row.append(block);
      }list.append(row);
    }
    if(!list.children.length)list.append(host.node('p','仓库池为空。添加公开GitHub仓库后，准备完成的版本可用于新建对话。','context-note'));
    $('close-repositories').textContent=returnToNew?'返回新建对话':'完成';busy();
  }
  function busy(){
    const locked=host.frozen();
    for(const b of document.querySelectorAll('.item-menu'))b.disabled=locked;
    $('confirm-new-session').disabled=locked || selection.length<1 || selection.length>8 || selection.some(id=>!available().some(r=>r.id===id));
    $('new-add-repository').disabled=locked;
    for(const input of document.querySelectorAll('#new-repo-options input'))input.disabled=locked;
    $('add-repository').disabled=locked || !!host.repositoryJob;
    $('cancel-repository-job').hidden=!host.repositoryJob;
    $('retry-repository-job').hidden=!host.repositoryJob;
    if($('welcome-start'))$('welcome-start').disabled=locked || !available().length;
    if($('welcome-add')){$('welcome-add').disabled=locked;$('welcome-add').hidden=!!host.activeSession;}
  }
  async function init(dependencies){
    host=dependencies;
    Object.assign(host.errors,{repository_not_in_pool:'仓库已归档、在回收站或尚未准备好，请核对仓库池。',session_read_only:'此对话只读，恢复后可继续。',organization_conflict:'内容已在其他页面更新，请重新打开后重试。',organization_task_running:'相关任务正在执行，请完成后再整理。',use_repository_trash:'请使用仓库池中的回收站操作。'});
    command=host.storageGet('management-command');$('retry-management').hidden=!command;
    if(command){$('organization-menu').append(host.node('h3','上次管理操作结果尚未确认'));$('organization-action').showModal();}
    $('show-archive').addEventListener('click',()=>openLibrary('archived'));$('show-trash').addEventListener('click',()=>openLibrary('trashed'));
    $('library-kind').addEventListener('change',()=>libraryPage());$('more-library').addEventListener('click',()=>libraryPage(true));$('close-library').addEventListener('click',()=>$('organization-library').close());
    $('close-action').addEventListener('click',()=>$('organization-action').close());$('retry-management').addEventListener('click',()=>command&&dispatch(command));
    $('organization-form').addEventListener('submit',async e=>{e.preventDefault();if(actionTarget)await dispatch({...actionTarget,request_id:uid(),title:$('organization-title').value});});
    for(const b of document.querySelectorAll('[data-pool-tab]'))b.addEventListener('click',()=>showPoolTab(b.dataset.poolTab));
    $('new-add-repository').addEventListener('click',addForNew);$('close-new-session').addEventListener('click',cancelNew);
    $('new-session-dialog').addEventListener('cancel',()=>{creatingDraft=false;returnToNew=false;});
    $('repository-manager').addEventListener('cancel',e=>{if(returnToNew){e.preventDefault();closePool();}});
    $('mobile-sidebar').addEventListener('click',()=>toggleSidebar(true));$('close-sidebar').addEventListener('click',()=>toggleSidebar(false));$('sidebar-shade').addEventListener('click',()=>toggleSidebar(false));
    initResize();toggleSidebar(false);await refreshPool();
  }
  let drag=null,preferredWidth=280;
  function toggleSidebar(open){const narrow=(globalThis.innerWidth||1280)<=720;document.body.classList.toggle('sidebar-open',open);$('sidebar-shade').hidden=!open;$('mobile-sidebar').setAttribute('aria-expanded',String(open));$('app-sidebar').inert=narrow&&!open;$('app-sidebar').setAttribute('aria-hidden',String(narrow&&!open));}
  function applyWidth(value){const maximum=Math.max(210,Math.min(400,(globalThis.innerWidth || 1280)-480));const width=Math.max(210,Math.min(maximum,value));document.documentElement.style.setProperty('--sidebar-width',`${width}px`);$('sidebar-resize').setAttribute('aria-valuenow',String(Math.round(width)));}
  function rememberWidth(){try{localStorage.setItem('repoagent-sidebar-width',String(preferredWidth));}catch{}}
  function initResize(){
    try{const raw=localStorage.getItem('repoagent-sidebar-width'),n=Number(raw);if(raw!==null && Number.isFinite(n)&&n>=210&&n<=400)preferredWidth=n;}catch{}
    const handle=$('sidebar-resize');applyWidth(preferredWidth);
    const finish=(cancel=false)=>{if(!drag)return;const old=drag;drag=null;document.body.classList.remove('resizing');if(cancel)preferredWidth=old.preference;else rememberWidth();applyWidth(preferredWidth);if(handle.hasPointerCapture?.(old.id))handle.releasePointerCapture(old.id);};
    handle.addEventListener('pointerdown',e=>{if(e.button!==0 || (globalThis.innerWidth || 1280)<=720)return;e.preventDefault();drag={id:e.pointerId,x:e.clientX,width:document.querySelector('.sidebar').getBoundingClientRect().width,preference:preferredWidth};handle.focus();handle.setPointerCapture(e.pointerId);document.body.classList.add('resizing');});
    handle.addEventListener('pointermove',e=>{if(!drag || e.pointerId!==drag.id)return;preferredWidth=Math.max(210,Math.min(400,(globalThis.innerWidth||1280)-480,drag.width+e.clientX-drag.x));applyWidth(preferredWidth);});
    handle.addEventListener('pointerup',()=>finish());handle.addEventListener('pointercancel',()=>finish(true));handle.addEventListener('lostpointercapture',()=>finish(true));
    handle.addEventListener('dblclick',()=>{preferredWidth=280;rememberWidth();applyWidth(preferredWidth);});
    handle.addEventListener('keydown',e=>{if(e.key==='Escape'){finish(true);return;}if(!['ArrowLeft','ArrowRight','Home'].includes(e.key))return;e.preventDefault();preferredWidth=e.key==='Home'?280:Math.max(210,Math.min(400,preferredWidth+(e.key==='ArrowRight'?10:-10)));rememberWidth();applyWidth(preferredWidth);});
    globalThis.addEventListener?.('resize',()=>{if(drag)finish(true);applyWidth(preferredWidth);toggleSidebar(globalThis.innerWidth>720?false:document.body.classList.contains('sidebar-open'));});
  }

  return {init,available,chosen,setSelection,renderScope,refreshPool,loadSessions,renderSessions,onSession,openNew,cancelNew,created,closePool,repositoryReady,renderRepositories,dispatch,busy};
})();
