// Documentation workspace: source selection is captured before opening the dialog.
(() => {
  const section=document.createElement('section');section.className='generated-documents';
  section.innerHTML='<div class="documents-heading"><div><span class="page-eyebrow">DOCUMENTATION GENERATOR</span><h2>Документы по вашему коду</h2><p>От выбранного метода до обзора конфигурации. С источниками и границами проверки.</p></div><button type="button" class="primary" id="create-document">＋ Создать документ</button></div><div id="generated-document-list" class="generated-document-list"></div>';
  $('#dashboard-docs .page-description').after(section);
  const button=document.createElement('button');button.type='button';button.id='document-code';button.textContent='▤ Документировать';button.title='Создать документацию по выделенному коду или модулю';
  $('#copy').after(button);
  const dialog=document.createElement('dialog');dialog.id='documentation-dialog';dialog.setAttribute('aria-labelledby','documentation-window-title');
  dialog.innerHTML=`<header class="documentation-dialog-heading"><div><span class="page-eyebrow">ATHENA · DOCUMENTATION</span><h2 id="documentation-window-title">Создание документации</h2></div><button type="button" id="close-documentation" aria-label="Закрыть документацию">×</button></header>
  <div class="documentation-workspace"><form id="documentation-form" class="documentation-options">
  <label for="document-title">Название</label><input id="document-title" autofocus maxlength="160" required placeholder="Например, архитектура конфигурации">
  <label for="document-kind">Тип документа</label><select id="document-kind"><option>Архитектура</option><option>Возможности</option><option>Описание кода</option><option>Разработка</option><option>Развёртывание</option><option>Известные проблемы</option></select>
  <label for="document-scope">Исходники</label><select id="document-scope"><option value="project">Текущая конфигурация</option><option value="module">Открытый модуль</option><option value="selection">Выделенный код</option></select>
  <p id="document-source-description" class="muted"></p><pre id="document-source-preview" tabindex="0" hidden></pre>
  <label for="document-goal">Что описать <span class="muted">· необязательно</span></label><textarea id="document-goal" maxlength="2000" rows="4" placeholder="Для кого документ, какие связи и детали важны…"></textarea>
  <p class="documentation-skill-note">▤ Documentation Generator<br><span>Ваш скилл и шаблон · русский · Markdown</span></p>
  <button class="primary" id="generate-document" disabled>Создать документацию ↗</button><button type="button" id="cancel-document-generation" hidden>Остановить генерацию</button>
  <p id="document-status" role="status" aria-live="polite"></p></form>
  <section class="documentation-result"><div class="documentation-result-toolbar"><div class="documentation-result-tabs"><button type="button" id="document-preview-tab" aria-pressed="true">Просмотр</button><button type="button" id="document-edit-tab" aria-pressed="false">Markdown</button></div><div><button type="button" id="save-document" disabled>Сохранить</button><button type="button" id="download-document" disabled>Скачать .md ↓</button><button type="button" id="download-document-pdf" disabled>Скачать PDF ↓</button></div></div>
  <div id="document-warnings" role="status"></div><article id="document-preview" class="ai-markdown"><div class="documentation-empty"><span>▤</span><h3>Документ начнётся с исходников</h3><p>Выберите область и тип. Athena создаст черновик, который можно проверить и отредактировать.</p></div></article><textarea id="document-markdown" spellcheck="false" aria-label="Текст документа Markdown" hidden></textarea><details id="document-sources" hidden><summary>Использованные исходники</summary><div></div></details></section></div>`;
  document.body.append(dialog);
  let capture=null,context=null,current=null,timer=null,editing=false,opening=0;
  const status=text=>{$('#document-status').textContent=text;};
  const setEditing=value=>{editing=value;$('#document-markdown').hidden=!value;$('#document-preview').hidden=value;$('#document-preview-tab').setAttribute('aria-pressed',String(!value));$('#document-edit-tab').setAttribute('aria-pressed',String(value));if(!value&&current)renderAIMarkdown($('#document-preview'),$('#document-markdown').value,new Map());};
  function captureCode(){
    if(!file?.editable||!currentFileId)return null;
    const input=$('#code-editor'),text=editingMode?input.value:file.content;
    return {file_id:currentFileId,path:file.path,hash:file.hash,draft:text,start:editingMode?input.selectionStart:0,end:editingMode?input.selectionEnd:0};
  }
  function sourceDescription(){
    const scope=$('#document-scope').value,preview=$('#document-source-preview');
    preview.hidden=scope==='project';
    if(scope==='project'){$('#document-source-description').textContent='Сохранённые метаданные и BSL-модули. Полнота контекста будет указана в результате.';return;}
    const text=scope==='selection'?capture.draft.slice(capture.start,capture.end):capture.draft;
    const first=scope==='selection'?capture.draft.slice(0,capture.start).split('\n').length:1;
    $('#document-source-description').textContent=capture.path+' · строки '+first+'–'+(first+text.split('\n').length-1)+(cleanCode(capture.draft)!==cleanCode(file?.content||'')?' · черновик':'');
    preview.textContent=text.slice(0,3000)+(text.length>3000?'\n… Предпросмотр сокращён':'');
  }
  function showJob(job){
    current=job;const running=job.status==='running';
    status(job.error||job.phase);
    $('#generate-document').disabled=running||!context;$('#cancel-document-generation').hidden=!running;
    $('#save-document').disabled=job.status!=='ready';$('#download-document').disabled=job.status!=='ready';$('#download-document-pdf').disabled=job.status!=='ready';
    $('#document-title').value=job.title;$('#document-kind').value=job.kind;
    $('#document-warnings').replaceChildren();
    for(const warning of job.warnings||[]){const p=document.createElement('p');p.textContent=warning;$('#document-warnings').append(p);}
    if(job.content){$('#document-markdown').value=job.content;renderAIMarkdown($('#document-preview'),job.content,new Map());}
    const sources=$('#document-sources');sources.hidden=!job.sources?.length;sources.querySelector('div').replaceChildren();
    for(const s of job.sources||[]){const p=document.createElement('p');p.textContent=s.path+' · '+s.start_line+'–'+s.end_line+(s.draft?' · черновик':'')+(s.complete?'':' · фрагмент');sources.querySelector('div').append(p);}
    if(job.coverage){const p=document.createElement('p');p.textContent='Модулей в контексте: '+job.coverage.modules_included+'/'+job.coverage.modules_total;sources.querySelector('div').prepend(p);}
  }
  async function poll(){
    if(!current)return;
    try{const job=await api('documents/job?id='+encodeURIComponent(current.id));showJob(job);if(job.status==='running')timer=setTimeout(poll,1200);else await refreshList();}catch(e){status(e.message);}
  }
  async function open(fromCode=false,job=null){
    const version=++opening;clearTimeout(timer);capture=captureCode();context=null;current=null;setEditing(false);
    $('#documentation-form').reset();$('#document-warnings').replaceChildren();$('#document-source-preview').hidden=true;$('#document-sources').hidden=true;$('#document-markdown').value='';
    $('#document-preview').innerHTML='<div class="documentation-empty"><span aria-hidden="true">▤</span><h3>Документ начнётся с исходников</h3><p>Выберите область и тип. Athena создаст черновик, который можно проверить и отредактировать.</p></div>';
    for(const id of ['generate-document','save-document','download-document','download-document-pdf'])$('#'+id).disabled=true;
    $('#cancel-document-generation').hidden=true;
    $('#document-scope option[value="module"]').disabled=!capture;
    $('#document-scope option[value="selection"]').disabled=!capture||capture.start===capture.end;
    $('#document-scope').value=fromCode&&capture?(capture.start!==capture.end?'selection':'module'):'project';
    $('#document-kind').value=fromCode?'Описание кода':'Архитектура';
    $('#document-title').value=fromCode&&capture?'Описание '+capture.path.split('/').pop():'Архитектура конфигурации';sourceDescription();
    if(!dialog.open)dialog.showModal();status('Подготавливаем контекст проекта…');
    try{const c=await api('agent/context');if(version!==opening)return;if(!c.ready)throw new Error('Откройте проект и дождитесь индексации.');context=Object.fromEntries(['project','snapshot','workspace_revision'].map(k=>[k,c[k]]));status('Готово к генерации');$('#generate-document').disabled=false;if(job){showJob(job);if(job.status==='running')poll();}}catch(e){status(e.message);if(job)showJob(job);}
  }
  async function refreshList(){
    try{
      const data=await api('documents'),list=$('#generated-document-list');list.replaceChildren();
      if(!data.documents.length){const p=document.createElement('p');p.className='muted';p.textContent='Здесь будут храниться созданные документы текущего проекта.';list.append(p);}
      for(const item of data.documents){const b=document.createElement('button');b.type='button';b.className='dashboard-document';const icon=document.createElement('span');icon.className='document-icon';icon.textContent='▤';const body=document.createElement('span'),title=document.createElement('strong'),desc=document.createElement('small');title.textContent=item.title;desc.textContent=item.kind+' · '+new Date(item.created*1000).toLocaleDateString('ru-RU')+' · '+item.phase;body.append(title,desc);const arrow=document.createElement('span');arrow.textContent='Открыть ↗';b.append(icon,body,arrow);b.onclick=async()=>{try{await open(false,await api('documents/job?id='+item.id));}catch(e){toast(e.message);}};list.append(b);}
    }catch(e){$('#generated-document-list').textContent=e.message;}
  }
  $('#documentation-form').onsubmit=async event=>{
    event.preventDefault();if(!context)return;
    if(current?.status==='running')return;
    const scope=$('#document-scope').value;
    const body={title:$('#document-title').value,kind:$('#document-kind').value,goal:$('#document-goal').value,scope,context};
    if(scope!=='project')Object.assign(body,{file_id:capture.file_id,hash:capture.hash,draft:capture.draft,start:capture.start,end:capture.end});
    $('#generate-document').disabled=true;$('#save-document').disabled=true;$('#download-document').disabled=true;$('#download-document-pdf').disabled=true;setEditing(false);$('#document-preview').textContent='Athena создаёт документ по выбранным исходникам…';$('#document-warnings').replaceChildren();$('#document-sources').hidden=true;status('Запускаем генерацию…');
    try{const started=await api('documents/start',body);current={id:started.id,status:'running'};await poll();}catch(e){status(e.message);$('#generate-document').disabled=false;}
  };
  $('#document-scope').onchange=sourceDescription;
  $('#create-document').onclick=()=>open();button.onclick=()=>open(true);
  $('#close-documentation').onclick=()=>dialog.close();
  $('#document-preview-tab').onclick=()=>setEditing(false);$('#document-edit-tab').onclick=()=>setEditing(true);
  $('#cancel-document-generation').onclick=async()=>{try{showJob(await api('documents/cancel',{id:current.id}));await refreshList();}catch(e){status(e.message);}};
  $('#save-document').onclick=async()=>{try{showJob(await api('documents/save',{id:current.id,content:$('#document-markdown').value}));status('Документ сохранён в библиотеке проекта');}catch(e){status(e.message);}};
  $('#download-document').onclick=()=>{
    const audit='\n\n---\n\n## Данные генерации Athena\n\n'+(current.warnings||[]).map(w=>'- '+w).join('\n')+'\n\n'+(current.sources||[]).map(s=>'- `'+s.path+'`, строки '+s.start_line+'–'+s.end_line+'; SHA-256: `'+s.hash+'`'+(s.draft?' (черновик)':'')).join('\n');
    const blob=new Blob([$('#document-markdown').value+audit],{type:'text/markdown;charset=utf-8'}),url=URL.createObjectURL(blob),a=document.createElement('a');a.href=url;a.download=(current.title.replace(/[<>:"/\\|?*\x00-\x1f]/g,'_')||'Документация')+'.md';a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);
  };
  $('#download-document-pdf').onclick=async()=>{
    if(current?.status!=='ready')return;
    const id=current.id,version=opening,b=$('#download-document-pdf');b.disabled=true;status('Подготавливаем PDF…');
    try{
      const response=await fetch('/api/documents/pdf',{method:'POST',headers:{'Content-Type':'application/json','X-Athena-Token':token},body:JSON.stringify({id,content:$('#document-markdown').value})});
      if(!response.ok){const error=await response.json();throw new Error(error.error||'Не удалось создать PDF');}
      const blob=await response.blob();if(version!==opening||current?.id!==id)return;
      const url=URL.createObjectURL(blob),a=document.createElement('a');a.href=url;a.download=(current.title.replace(/[<>:"/\\|?*\x00-\x1f]/g,'_')||'Документация')+'.pdf';a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);
      status('PDF подготовлен для скачивания');
    }catch(e){if(version===opening)status(e.message);}
    finally{if(version===opening&&current?.id===id)b.disabled=current.status!=='ready';}
  };
  $('#nav-docs').addEventListener('click',refreshList);
  document.addEventListener('athena-project-changed',()=>{opening++;context=null;dialog.close();clearTimeout(timer);refreshList();});
  refreshList();
})();
