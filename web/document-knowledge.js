// Uploaded project references are distinct from generated documentation drafts.
(() => {
  const card=document.querySelector('.documentation-import-card:last-child');
  card.className='knowledge-documents';
  card.innerHTML=`<div class="knowledge-documents-heading"><div><span class="page-eyebrow">PROJECT KNOWLEDGE</span><h3>База знаний агента</h3><p>Архитектура, инструкции и стандарты вашей конфигурации.</p></div><span class="knowledge-local">Локальные эмбеддинги</span></div>
  <form id="knowledge-upload-form"><label class="knowledge-file-label">Документ<input type="file" id="knowledge-upload-file" accept=".md,.txt,.pdf,.docx" required></label><label class="knowledge-rule-option"><input type="checkbox" id="knowledge-upload-rules"><span>Обязательные правила проекта<small>Весь текст будет закреплён в контексте агента на каждом шаге. Отмечайте только проверенные инструкции.</small></span></label><button class="primary" id="knowledge-upload-submit">Загрузить и индексировать ↗</button></form>
  <p class="knowledge-upload-help">MD, TXT, PDF с текстовым слоем, DOCX · до 2 МБ. Правила — до 12 000 символов суммарно. Эмбеддинги строятся автоматически с настройками CPU/GPU рабочего места.</p><p id="knowledge-upload-status" role="status"></p><div id="knowledge-document-list"></div>`;
  let context=null,version=0,busy=false;
  const notice=text=>{$('#knowledge-upload-status').textContent=text;};
  async function refresh(){
    const ticket=++version;
    try{
      const c=await api('agent/context');if(ticket!==version)return;context=c.ready?c:null;
      $('#knowledge-upload-submit').disabled=busy||!context;
      if(!context){$('#knowledge-document-list').textContent='Откройте проект и дождитесь индексации, чтобы загрузить документы.';return;}
      const data=await api('document-knowledge');if(ticket!==version)return;
      const list=$('#knowledge-document-list');list.replaceChildren();
      if(!data.documents.length){list.textContent='Загруженные документы появятся здесь. Агент найдёт нужные фрагменты по вашей задаче.';return;}
      for(const item of data.documents){
        const row=document.createElement('article');row.className='knowledge-document-row';
        const heading=document.createElement('div'),title=document.createElement('strong'),meta=document.createElement('small');
        title.textContent=item.name;meta.textContent=(item.rules?'Правила проекта':'Справочный документ')+' · '+item.characters.toLocaleString('ru-RU')+' символов';heading.append(title,meta);
        const state=document.createElement('p');state.className='knowledge-document-state';state.textContent=!item.enabled?'Отключён':item.status==='error'?'Ошибка: '+item.error:item.status==='ready'?'Эмбеддинги готовы · фрагментов: '+item.done:(item.status==='indexing'?'Индексирование':'В очереди')+' · '+item.done+' / '+item.total;
        const progress=document.createElement('progress');progress.max=item.total||1;progress.value=item.done;progress.hidden=!item.enabled||item.status==='ready'||item.status==='error';progress.setAttribute('aria-label','Индексация '+item.name);
        const actions=document.createElement('div');actions.className='knowledge-document-actions';
        async function change(values){try{await api('document-knowledge/configure',{project:c.project,id:item.id,enabled:!!item.enabled,rules:!!item.rules,...values});await refresh();}catch(e){notice(e.message);}}
        const rule=document.createElement('label'),check=document.createElement('input');check.type='checkbox';check.checked=!!item.rules;check.setAttribute('aria-label','Обязательные правила: '+item.name);check.onchange=()=>change({rules:check.checked});rule.append(check,' Правила проекта');
        const enabled=document.createElement('button');enabled.type='button';enabled.textContent=item.enabled?'Отключить':'Включить';enabled.setAttribute('aria-label',enabled.textContent+' '+item.name);enabled.onclick=()=>change({enabled:!item.enabled});
        const read=document.createElement('button');read.type='button';read.textContent='Прочитать';read.setAttribute('aria-label','Прочитать '+item.name);read.onclick=async()=>{try{const data=await api('document-knowledge/read?id='+item.id);const dialog=document.createElement('dialog');dialog.className='knowledge-read-dialog';const h=document.createElement('h2');h.textContent=item.name;const p=document.createElement('pre');p.textContent=data.content;const close=document.createElement('button');close.textContent='Закрыть';close.onclick=()=>dialog.close();dialog.append(h,p,close);dialog.addEventListener('close',()=>dialog.remove());document.body.append(dialog);dialog.showModal();}catch(e){notice(e.message);}};
        actions.append(read,rule,enabled);
        if(item.status==='error'){const retry=document.createElement('button');retry.type='button';retry.textContent='Повторить';retry.onclick=()=>change({});actions.append(retry);}
        row.append(heading,state,progress,actions);list.append(row);
      }
    }catch(e){if(ticket===version)notice(e.message);}
  }
  $('#knowledge-upload-form').onsubmit=async event=>{
    event.preventDefault();const file=$('#knowledge-upload-file').files[0];if(!file||!context||busy)return;
    if(file.size>2000000){notice('Размер файла — до 2 МБ.');return;}
    const project=context.project,rules=$('#knowledge-upload-rules').checked;busy=true;$('#knowledge-upload-submit').disabled=true;notice('Загружаем документ…');
    try{
      const data=await new Promise((resolve,reject)=>{const reader=new FileReader();reader.onload=()=>resolve(reader.result.split(',')[1]);reader.onerror=()=>reject(new Error('Не удалось прочитать файл.'));reader.readAsDataURL(file);});
      await api('document-knowledge/upload',{project,name:file.name,data,rules});$('#knowledge-upload-form').reset();notice('Документ загружен. Локальная индексация началась автоматически.');
    }catch(e){notice(e.message);}finally{busy=false;await refresh();}
  };
  $('#nav-docs').addEventListener('click',refresh);
  document.addEventListener('athena-project-changed',()=>{context=null;notice('');refresh();});
  setInterval(()=>{if(document.body.dataset.page==='docs'&&!busy&&!document.querySelector('.knowledge-read-dialog[open]'))refresh();},3000);
  refresh();
})();
