(() => {
  const card=document.querySelector('.documentation-import-card');
  if(!card)return;
  card.className='knowledge-documents git-projects';
  card.innerHTML=`<div class="knowledge-documents-heading"><div><span class="page-eyebrow">GIT SOURCES</span><h3>Git-проекты</h3><p>Код 1С, документация и связанные проекты в контексте нейросети.</p></div></div>
    <form id="git-connect"><label>Адрес репозитория<input name="url" required placeholder="https://github.com/team/project.git или git@host:team/project.git"></label><label>Название<input name="name" maxlength="120" placeholder="Необязательно"></label><button class="primary">Скачать и подключить</button></form>
    <p class="field-hint">Можно подключить несколько источников к открытому проекту. Для доступа используются Git Credential Manager или SSH. Пароли и токены в URL не поддерживаются.</p>
    <p id="git-notice" role="status"></p><div id="git-list"></div>`;
  let context=null,busy=false,ticket=0;
  const notice=text=>$('#git-notice').textContent=text;
  const element=(tag,text)=>{const e=document.createElement(tag);if(text!==undefined)e.textContent=text;return e;};
  async function refresh(){
    if(busy)return;
    const version=++ticket;
    try{
      const c=await api('agent/context');if(version!==ticket)return;context=c.ready?c:null;
      $('#git-connect button').disabled=!context;
      if(!context){$('#git-list').textContent='Откройте основной проект 1С и дождитесь индексации.';return;}
      const data=await api('git-projects');if(version!==ticket)return;
      const list=$('#git-list');list.replaceChildren();
      if(!data.repositories.length){list.textContent='Подключённые репозитории появятся здесь.';return;}
      for(const repo of data.repositories){
        const row=element('article');row.className='git-repository';
        row.append(element('h4',repo.name),element('small',repo.url),element('p',`${repo.branch||'Нет ветки'} · ${repo.head?.slice(0,10)||'Нет коммитов'} · файлов в контексте: ${repo.files||0} · исходящих: ${repo.ahead||0} · входящих: ${repo.behind||0}`));
        row.append(element('small','Клон: '+repo.path));
        row.append(element('p',(repo.error||repo.warning||'')+(repo.checked?' · Проверено: '+new Date(repo.checked*1000).toLocaleString('ru-RU'):'')));
        const controls=element('div');controls.className='knowledge-document-actions';
        const button=(text,fn)=>{const b=element('button',text);b.type='button';b.onclick=fn;controls.append(b);return b;};
        const action=(action,extra={})=>api('git-projects/action',{project:c.project,id:repo.id,action,...extra});
        const run=async(fn,message)=>{
          if(busy)return;busy=true;card.setAttribute('aria-busy','true');card.querySelectorAll('button,input').forEach(e=>e.disabled=true);notice(message||'Выполняем операцию Git…');
          try{await fn();notice('Готово.');}catch(e){notice(e.message);}finally{busy=false;card.removeAttribute('aria-busy');await refresh();}
        };
        for(const [key,label] of [['enabled','Учитывать в ответах'],['automatic','Обновлять из Git каждую минуту']]){
          const l=element('label'),check=element('input');check.type='checkbox';check.checked=!!repo[key];check.onchange=()=>run(()=>action('configure',{enabled:!!repo.enabled,automatic:!!repo.automatic,[key]:check.checked}));l.append(check,' '+label);controls.append(l);
        }
        button('Получить изменения',()=>run(()=>action('sync')));
        button('Обновить локальный индекс',()=>run(()=>action('refresh')));
        button('Новая ветка',()=>{const branch=prompt('Имя новой ветки','athena/changes');if(branch)run(()=>action('branch',{branch}));});
        button('Копировать открытый проект',()=>{const prefix=prompt('Каталог в репозитории (пусто — корень). Файлы открытой выгрузки будут скопированы с заменой совпадающих путей; после копирования проверьте diff.','configuration');if(prefix!==null)run(()=>action('import-current',{prefix}));});
        button('Редактировать / создать файл',()=>edit(repo,c.project,run));
        button('Push в origin',()=>{if(confirm(`Отправить коммиты ветки ${repo.branch} в origin?\n${repo.url}\nКоммит: ${repo.head}`))run(()=>action('push',{head:repo.head,branch:repo.branch}));});
        button('Отключить',()=>{if(confirm('Отключить источник от проекта? Локальный клон сохранится.'))run(()=>action('disconnect'));});
        row.append(controls);
        if(repo.changes.length){
          const selection=element('div');selection.className='git-changes';
          for(const change of repo.changes){const l=element('label'),check=element('input');check.type='checkbox';check.value=change.path;l.append(check,` ${change.status} ${change.path}`);selection.append(l);}
          const review=element('button','Просмотреть выбранные файлы и создать коммит');review.type='button';
          review.onclick=async()=>{
            const paths=[...selection.querySelectorAll('input:checked')].map(e=>e.value);
            try{
              const data=await action('review',{paths});
              const dialog=element('dialog');dialog.className='knowledge-read-dialog';
              const pre=element('pre',data.diff),message=element('input');message.placeholder='Сообщение коммита';message.setAttribute('aria-label','Сообщение коммита');
              const status=element('p'),commit=element('button','Создать коммит'),close=element('button','Закрыть');
              commit.onclick=async()=>{commit.disabled=true;try{await action('commit',{paths:data.paths,token:data.token,message:message.value});dialog.close();notice('Коммит создан. Для отправки нажмите Push в origin.');await refresh();}catch(e){status.textContent=e.message;}finally{commit.disabled=false;}};
              close.onclick=()=>dialog.close();dialog.append(element('h2','Проверка изменений'),pre,message,status,commit,close);dialog.onclose=()=>dialog.remove();document.body.append(dialog);dialog.showModal();
            }catch(e){notice(e.message);}
          };
          row.append(selection,review);
        }
        list.append(row);
      }
    }catch(e){if(version===ticket)notice(e.message);}
  }
  async function edit(repo,project,run){
    const path=prompt('Путь файла в репозитории, например docs/architecture.md');if(!path)return;
    const create=confirm('Открыть существующий файл? Нажмите «Отмена», чтобы создать новый.');
    let data={content:'',hash:null};
    try{if(create)data=await api('git-projects/action',{project,id:repo.id,action:'read',path});}catch(e){notice(e.message);return;}
    const dialog=element('dialog');dialog.className='knowledge-read-dialog';const text=element('textarea');text.value=data.content;text.rows=22;text.setAttribute('aria-label',path);
    const save=element('button','Сохранить в локальный клон'),close=element('button','Закрыть'),status=element('p');
    save.onclick=async()=>{save.disabled=true;try{await api('git-projects/action',{project,id:repo.id,action:'write',path,content:text.value,expected_hash:data.hash});dialog.close();notice('Файл сохранён. Просмотрите diff и создайте коммит.');await refresh();}catch(e){status.textContent=e.message;}finally{save.disabled=false;}};
    close.onclick=()=>dialog.close();dialog.append(element('h2',path),text,status,save,close);dialog.onclose=()=>dialog.remove();document.body.append(dialog);dialog.showModal();
  }
  $('#git-connect').onsubmit=async event=>{
    event.preventDefault();if(!context||busy)return;
    const data=new FormData(event.target),project=context.project;busy=true;$('#git-connect button').disabled=true;notice('Клонируем репозиторий и индексируем материалы…');
    try{await api('git-projects/connect',{project,url:data.get('url'),name:data.get('name')});event.target.reset();notice('Репозиторий подключён. Материалы доступны в контексте нейросети.');}
    catch(e){notice(e.message);}finally{busy=false;await refresh();}
  };
  $('#nav-docs').addEventListener('click',refresh);
  document.addEventListener('athena-project-changed',()=>{ticket++;context=null;refresh();});
  setInterval(()=>{if(document.body.dataset.page==='docs'&&!document.querySelector('.knowledge-read-dialog[open]'))refresh();},15000);
  refresh();
})();
