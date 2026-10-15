const $ = s => document.querySelector(s);
const token = $('meta[name="athena-token"]').content;
let project = null, nodes = new Map(), selected = null, file = null, currentFileId = null;
let loading = false, matches = [], matchIndex = -1, fileVersion = 0;
let connectionData = {profiles: [], bases: [], platforms: [], active: null};
let availableFiles = [];
let activeTab = 'overview';
let openDocuments = [], creationVisible = false, creationOpen = false, rootObjects = new Map();
let projectRevision = 0, autoState = {enabled:false, interval:60, phase:'Выключено'}, polling = false;
let autoFormError = '';
let editingMode = false, editDraft = null, preparedUpload = null, uploading = false, rebaseMode = false;
const editorDrafts = new Map(), discardedDrafts = new Map();
function plural(n, forms) {const last = n % 10, lastTwo = n % 100; return `${n} ${forms[lastTwo >= 11 && lastTwo <= 14 ? 2 : last === 1 ? 0 : last >= 2 && last <= 4 ? 1 : 2]}`;}
const escapeHtml = s => String(s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
async function api(path, body) {
  const response = await fetch('/api/' + path, {method: body === undefined ? 'GET' : 'POST', headers: {'X-Athena-Token': token, 'Content-Type':'application/json'}, body: body === undefined ? undefined : JSON.stringify(body)});
  const data = await response.json();
  if (!response.ok) {if(response.status===403&&data.error==='Требуется вход в аккаунт.')location.replace('/login');throw Error(data.error || 'Ошибка запроса');}
  return data;
}
function toast(text) { $('#toast').textContent = text; $('#toast').hidden = false; clearTimeout(toast.timer); toast.timer = setTimeout(() => $('#toast').hidden = true, 6000); }
function showDialog() { $('#dialog-error').textContent = ''; $('#folder-path').value = project?.root || ''; $('#project-dialog').showModal(); $('#folder-path').focus(); }
$('#open-project').onclick = $('#welcome-open').onclick = showDialog;
$('#close-dialog').onclick = () => $('#project-dialog').close();
$('#choose-folder').onclick = async () => {
  $('#choose-folder').disabled = true;
  try { const result = await api('choose', {}); if (result.path) $('#folder-path').value = result.path; }
  catch (e) { $('#dialog-error').textContent = e.message; }
  finally { $('#choose-folder').disabled = false; }
};
$('#project-form').onsubmit = e => {e.preventDefault(); openProject($('#folder-path').value);};
$('#demo').onclick = () => openProject('__DEMO__');
$('#refresh').onclick = async () => {
  const profile = connectionData.profiles.find(p => p.id === connectionData.active);
  if (profile) {await showExportDialog(profile); if (profile.os_auth || profile.has_password || !profile.user) startExport(exportValues());}
  else if (project) openProject(project.root);
};
function busy(value) { if(!value)$('#index-progress').hidden=true; loading = value; $('#auto-settings').disabled = $('#load-project').disabled = $('#demo').disabled = $('#open-project').disabled = $('#export-project').disabled = value; $('#refresh').disabled = $('#create-object').disabled = value || !project; }
async function openProject(path) {
  if (loading) return;
  busy(true); $('#dialog-error').textContent = '';
  try {
    await api('open', {path});
    $('#project-dialog').close();
    await waitForLoad();
  } catch (e) { $('#status').textContent = 'Не удалось открыть проект'; toast(e.message); if ($('#project-dialog').open) $('#dialog-error').textContent = e.message; }
  finally { busy(false); }
}
async function waitForLoad(preserve = false) {
  while (true) {
    const state = await api('state');
    const job = state.job;
    updateIndexProgress(job);
    if (job.operation === 'export') {
      $('#export-progress').hidden = false;
      $('#export-progress').textContent = `${job.phase || 'Выгрузка'}${job.total ? ` · ${job.done} / ${job.total}` : ''}${job.elapsed ? ` · ${Math.floor(job.elapsed / 60)} мин ${job.elapsed % 60} с` : ''}`;
      $('#cancel-export').hidden = !job.cancellable;
      if (job.log) {$('#export-log').hidden = false; $('#export-log-text').textContent = job.log;}
    }
    if (job.status === 'error' || job.status === 'cancelled') throw Error(job.error);
    if (job.status === 'ready') {
      await refreshConnections();
      await updateProject(state, preserve);
      return;
    }
    $('#status').textContent = `${job.phase || 'Загрузка'}${job.total ? ` · ${job.done} / ${job.total}` : '…'}${job.elapsed!==undefined ? ` · ${Math.floor(job.elapsed/60)} мин ${job.elapsed%60} с` : ''}`;
    await new Promise(resolve => setTimeout(resolve, 250));
  }
}
async function updateProject(state, preserve = true) {
  const snapshot = await api('project'); if (!snapshot) return;
  const previous = {id:selected, tab:activeTab, file:currentFileId, scroll:$('#source').scrollTop,
    editor:{start:$('#code-editor').selectionStart,end:$('#code-editor').selectionEnd,scroll:$('#code-editor').scrollTop,focused:document.activeElement===$('#code-editor')},
    group:graphGroup, page:graphPage, camera:{...graphCamera}, points:new Map(graphPoints.map(p=>[p.key,{x:p.x,y:p.y}]))};
  if(project?.root !== snapshot.root){openDocuments=[];closeCreation(true);}
  project = snapshot; nodes = new Map(project.nodes.map(n => [n.id,n]));
  rootObjects=new Map(project.nodes.filter(n=>n.parent===project.configuration && n.kind!=='Subsystem').map(n=>[`${n.kind}.${n.name}`,n]));
  projectRevision = state.revision || 0;
  $('#project-path').textContent = project.root; $('#project-path').title = project.root;
  $('#object-count').textContent = project.stats.objects;
  $('#stats').textContent = [plural(project.stats.objects, ['объект','объекта','объектов']), plural(project.stats.modules, ['модуль','модуля','модулей']), plural(project.stats.files, ['файл','файла','файлов'])].join(' · ');
  const keep = preserve && nodes.has(previous.id);
  if (!preserve) $('#tree-search').value = '';
  selectNode(keep ? previous.id : project.configuration, keep ? previous.tab : undefined);
  if (keep && previous.tab === 'code' && previous.file && availableFiles.some(f=>f.id===previous.file)) {
    $('#files').value = previous.file; await loadFile(); $('#source').scrollTop = previous.scroll;
    if(editingMode && file?.editable){$('#code-editor').setSelectionRange(previous.editor.start,previous.editor.end);$('#code-editor').scrollTop=previous.editor.scroll;if(previous.editor.focused)$('#code-editor').focus();}
  }
  if (keep && previous.tab === 'graph') {
    graphGroup=previous.group;graphPage=previous.page;renderGraph();
    const same=graphPoints.length===previous.points.size && graphPoints.every(p=>previous.points.has(p.key));
    if(same){for(const p of graphPoints)Object.assign(p,previous.points.get(p.key));graphCamera=previous.camera;updateGraphPositions();updateGraphCamera();}
  }
  renderRecent(state.recent); renderAuto(state.auto);renderChanges();
  resetAIView();resetSearchResults();
  $('#status').textContent = preserve ? 'Проект автоматически обновлён · Локальный проект' : 'Проект открыт · Локальный проект';
  document.dispatchEvent(new CustomEvent('athena-project-changed'));
}
function renderAuto(state) {
  if(!state)return;autoState=state;
  $('#auto-settings').classList.toggle('enabled',state.enabled);
  $('#auto-settings').textContent=state.enabled?'Автообновление ●':'Автообновление';
  $('#auto-status').textContent=state.phase+(state.last_check?` · ${new Date(state.last_check*1000).toLocaleTimeString('ru')}`:'');
  $('#auto-error').textContent=autoFormError||state.error||'';
}
$('#auto-settings').onclick=()=>{
  autoFormError='';
  const profile=connectionData.profiles.find(p=>p.id===connectionData.active);
  $('#auto-connection').textContent=profile?`Подключение: ${profile.name}`:'Для автообновления сначала выгрузите конфигурацию из 1С.';
  $('#auto-enabled').checked=autoState.enabled;$('#auto-interval').value=String(autoState.interval);
  $('#auto-password').value='';$('#auto-password-row').hidden=!profile?.user || profile.os_auth || profile.has_password;
  renderAuto(autoState);$('#auto-dialog').showModal();
};
$('#close-auto').onclick=()=>$('#auto-dialog').close();
$('#apply-auto').onclick=async()=>{
  autoFormError='';
  $('#apply-auto').disabled=true;
  try{renderAuto(await api('auto',{enabled:$('#auto-enabled').checked,interval:Number($('#auto-interval').value),password:$('#auto-password').value}));$('#auto-password').value='';await refreshConnections();}
  catch(e){autoFormError=e.message;$('#auto-error').textContent=e.message;}
  finally{$('#apply-auto').disabled=false;}
};
async function pollAuto() {
  if(polling || loading)return;polling=true;
  try{
    const state=await api('state');renderAuto(state.auto);
    if(loading)return;
    if(project && state.revision && state.revision!==projectRevision && state.job.status==='ready'){
      await refreshConnections();await updateProject(state);
    }else if(autoState.enabled){$('#status').textContent=`Автообновление · ${state.job.operation==='autosync' && state.job.status==='running'?state.job.phase:autoState.phase}`;}
  }catch(e){if(autoState.enabled)$('#auto-status').textContent='Нет связи с локальным сервером. Повторная проверка…';}
  finally{polling=false;}
}
function renderRecent(paths) {
  $('#recent').replaceChildren();
  if (!paths.length) return;
  const heading = document.createElement('div'); heading.className = 'section-title'; heading.textContent = 'НЕДАВНИЕ ПРОЕКТЫ'; $('#recent').append(heading);
  for (const path of paths) {const button = document.createElement('button'); button.textContent = path; button.onclick = () => openProject(path); $('#recent').append(button);}
}
function renderTree(revealSelected = false) {
  if (!project) return;
  const query = $('#tree-search').value.toLocaleLowerCase('ru').trim();
  const visible = new Set();
  if (query) for (const node of nodes.values()) {
    if (`${node.name} ${node.label} ${node.properties.Synonym || ''}`.toLocaleLowerCase('ru').includes(query)) {
      let item = node; while (item && !visible.has(item.id)) {visible.add(item.id); item = nodes.get(item.parent);}
    }
  }
  if(query && $('#tree-mode').value==='subsystems')for(const node of nodes.values()){
    if(node.kind==='Subsystem' && subsystemObjects(node).some(n=>visible.has(n.id))){let item=node;while(item&&!visible.has(item.id)){visible.add(item.id);item=nodes.get(item.parent);}}
  }
  const previousOpen = new Set([...$('#tree').querySelectorAll('details[open]')].map(d => d.dataset.key));
  if (revealSelected) {
    let item = nodes.get(selected);
    while (item?.parent) {previousOpen.add(item.parent + item.label); item = nodes.get(item.parent);}
  }
  const container = $('#tree'); container.replaceChildren();
  function nodeRow(node) {
    const box = document.createElement('div');
    const button = document.createElement('button'); button.className = 'tree-node' + (selected === node.id ? ' selected' : ''); button.dataset.id = node.id;button.dataset.kind=node.kind;
    button.title = `${node.label}: ${node.name}`;
    const icon = document.createElement('span'); icon.className = 'node-icon'; icon.textContent = ({Catalog:'С',Document:'Д',Module:'М',CommonModule:'М',Form:'Ф',Attribute:'Р',TabularSection:'Т',InformationRegister:'РС',Configuration:'1С'})[node.kind] || 'О';
    button.append(icon, document.createTextNode(node.name)); button.onclick = () => selectNode(node.id); button.oncontextmenu=e=>{if(['Catalog','Document'].includes(node.kind)){e.preventDefault();openCreation(node.kind);}}; box.append(button);
    const children = node.children.map(id => nodes.get(id)).filter(n => !query || visible.has(n.id));
    if($('#tree-mode').value==='subsystems' && node.kind==='Subsystem'){
      for(const linked of subsystemObjects(node))if(!children.some(n=>n.id===linked.id) && (!query || visible.has(linked.id)))children.push(linked);
    }
    if($('#tree-mode').value==='subsystems' && node.id===project.configuration){
      const subsystems=children.filter(n=>n.kind==='Subsystem');
      if(subsystems.length)box.append(groupChildren(subsystems,node.id));
      else{const message=document.createElement('p');message.className='muted';message.textContent='В этой выгрузке нет подсистем. Переключитесь на «По типам».';box.append(message);}
      return box;
    }
    if (children.length) box.append(groupChildren(children, node.id));
    return box;
  }
  function groupChildren(children, prefix) {
    const groups = new Map();
    children.sort((a,b)=>metadataRank(a.kind)-metadataRank(b.kind) || a.name.localeCompare(b.name,'ru'));
    for (const node of children) {if (!groups.has(node.label)) groups.set(node.label, []); groups.get(node.label).push(node);}
    const wrapper = document.createElement('div'); wrapper.className = 'tree-children';
    for (const [label, members] of groups) {
      const details = document.createElement('details'); details.dataset.key = prefix + label;
      details.open = Boolean(query) || previousOpen.has(details.dataset.key);
      const summary = document.createElement('summary'); summary.append(document.createTextNode(groupLabels[members[0].kind] || label));
      if(prefix===project.configuration && ['Catalog','Document'].includes(members[0].kind)){const add=document.createElement('button');add.className='category-add';add.textContent='＋';add.title=`Создать: ${label}`;add.setAttribute('aria-label',`Создать: ${label}`);add.onclick=e=>{e.preventDefault();e.stopPropagation();openCreation(members[0].kind);};summary.append(add);}
      const count = document.createElement('span'); count.className = 'count'; count.textContent = members.length; summary.append(count); details.append(summary);
      let populated = false;
      function populate() {if (populated) return; populated = true; for (const node of members) details.append(nodeRow(node));}
      if (details.open) populate();
      details.ontoggle = () => {if (details.open) populate();};
      wrapper.append(details);
    }
    return wrapper;
  }
  if (query && !visible.size) {container.innerHTML = '<p class="muted">Ничего не найдено</p>'; return;}
  container.append(nodeRow(nodes.get(project.configuration)));
}
$('#tree-search').oninput = renderTree;
function ancestors(node) { const result = []; while (node) {result.unshift(node.name); node = nodes.get(node.parent);} return result; }
function card(node) {
  const button = document.createElement('button'); const small = document.createElement('small'); small.textContent = node.label;
  const strong = document.createElement('strong'); strong.textContent = node.name; button.append(small, strong); button.onclick = () => selectNode(node.id); return button;
}
function selectNode(id, preferredTab) {
  rememberDocument(); closeCreation();
  ++fileVersion; selected = id; file = null; currentFileId = null;
  editDraft=null;editorControls();
  const node = nodes.get(id); if (!node) return;
  if(!openDocuments.some(d=>d.id===id))openDocuments.push({id,tab:preferredTab||'overview',file:null});
  renderDocuments();renderInspector(node);
  $('#welcome').hidden = true; $('#explorer').hidden = false;
  $('#breadcrumb').textContent = ancestors(node).join(' / '); $('#kind').textContent = node.label;
  $('#object-name').textContent = node.name; $('#synonym').textContent = node.properties.Synonym || '';
  renderTree(true);
  const overview = $('#overview'); overview.replaceChildren();
  if (node.missing) {const warning = document.createElement('p'); warning.className = 'warning'; warning.textContent = 'Объект указан в конфигурации, но его XML отсутствует в выгрузке. Выполните полную выгрузку, чтобы увидеть свойства и код.'; overview.append(warning);}
  if (id === project.configuration && project.warnings.length) {
    const details = document.createElement('details'); details.className = 'warning'; const summary = document.createElement('summary'); summary.textContent = `Замечания при загрузке: ${project.warnings.length}`; details.append(summary);
    for (const warning of project.warnings) {const p = document.createElement('p'); p.textContent = warning; details.append(p);} overview.append(details);
  }
  $('#structure').replaceChildren();
  const structureTitle=document.createElement('h2');structureTitle.textContent='Структура объекта';$('#structure').append(structureTitle);
  const structureCards=document.createElement('div');structureCards.className='cards';for(const id of node.children.slice(0,120))structureCards.append(card(nodes.get(id)));$('#structure').append(structureCards);
  if(node.children.length>120){const p=document.createElement('p');p.className='muted';p.textContent=`Показаны первые 120 из ${node.children.length} элементов. Все объекты доступны в дереве и поиске.`;$('#structure').append(p);}
  if(!node.children.length){const empty=document.createElement('p');empty.className='muted';empty.textContent='У объекта нет вложенных элементов.';$('#structure').append(empty);}
  const heading = document.createElement('div'); heading.className = 'section-title'; heading.textContent = 'СВОЙСТВА ИЗ ВЫГРУЗКИ'; overview.append(heading);
  const table = document.createElement('table'); table.className = 'properties';
  for (const [key, val] of Object.entries(node.properties)) { const tr = document.createElement('tr'); const name = document.createElement('td'); name.textContent = propertyLabels[key] || key; const content = document.createElement('td'); content.textContent = val || '—'; tr.append(name, content); table.append(tr); }
  if (!Object.keys(node.properties).length) { const p = document.createElement('p'); p.className = 'muted'; p.textContent = 'У этого элемента нет отдельных свойств в выгрузке.'; overview.append(p); }
  else overview.append(table);
  const select = $('#files'); select.replaceChildren();
  // Include nested modules/forms so an object's code tab covers all of its source code.
  const available = new Map();
  function collect(item) {for (const f of item.files) available.set(f.id, f); for (const nested of item.children) collect(nodes.get(nested));}
  collect(node);
  const files = [...available.values()].sort((a,b) => (a.language !== 'bsl') - (b.language !== 'bsl') || a.path.localeCompare(b.path));
  availableFiles = files;renderModuleTabs();
  $('#file-count').textContent = files.length;
  $('#source').textContent = ''; $('#source-path').textContent = ''; $('#symbols').textContent = ''; $('#code-search').value = ''; $('#match-count').textContent = '';
  $('#copy').disabled = !files.length;
  graphGroup = null; graphPage = 0;
  switchTab(preferredTab || (activeTab === 'graph' ? 'graph' : node.kind === 'Module' ? 'code' : 'overview'));
}
function switchTab(tab) {
  activeTab = tab;
  $('.workspace').classList.toggle('graph-mode', tab === 'graph');
  $('#structure').hidden = tab !== 'structure';
  $('#overview').hidden = tab !== 'overview'; $('#code').hidden = tab !== 'code';
  $('#graph').hidden = tab !== 'graph';
  document.querySelectorAll('.tabs button').forEach(b => b.classList.toggle('active', b.dataset.tab === tab));
  rememberDocument();renderDocuments();
  editorControls();
  if (tab === 'graph') renderGraph();
  if (tab === 'code' && !file) {
    if (!$('#files').options.length) for (const f of availableFiles) {const option = document.createElement('option'); option.value = f.id; option.textContent = f.path; $('#files').append(option);}
    loadFile();
  }
}
document.querySelectorAll('.tabs button').forEach(b => b.onclick = () => switchTab(b.dataset.tab));
$('#files').onchange = loadFile;
async function loadFile() {
  const id = $('#files').value, request = ++fileVersion;
  file = null; currentFileId = null; $('#source').textContent = 'Загрузка файла…'; $('#symbols').replaceChildren(); $('#copy').disabled = true;
  editDraft=null;editorControls();
  if (!id) {$('#source').textContent = 'Для этого объекта нет исходных файлов.'; return;}
  try {
    const result = await api('file?id=' + encodeURIComponent(id));
    if (request !== fileVersion) return;
    file = result; currentFileId = id; $('#source-path').textContent = result.path; $('#code-search').value = ''; renderCode(); $('#copy').disabled = false;
    editingMode=!!file.editable;restoreDraft();editorControls();rememberDocument();renderDocuments();renderModuleTabs();
    for (const symbol of result.symbols) {const button = document.createElement('button'); button.textContent = symbol.name; const line = document.createElement('span'); line.textContent = symbol.line; button.append(line); button.onclick = () => jump(symbol.line); $('#symbols').append(button);}
    if (!result.symbols.length) $('#symbols').innerHTML = '<p class="muted">Нет процедур и функций</p>';
  } catch(e) {if (request === fileVersion) {$('#source').textContent = e.message; toast(e.message);}}
}
function highlight(line, bsl) {
  if (!bsl) return escapeHtml(line);
  const pattern = /\/\/.*$|"(?:""|[^"])*"|\b\d+(?:\.\d+)?\b|[А-Яа-яЁёA-Za-z_][А-Яа-яЁёA-Za-z_0-9]*/g;
  const keywords = new Set('Процедура Функция КонецПроцедуры КонецФункции Экспорт Если Тогда Иначе ИначеЕсли КонецЕсли Для Каждого Из По Цикл КонецЦикла Пока Возврат Перем Новый Истина Ложь Неопределено Попытка Исключение КонецПопытки ВызватьИсключение Продолжить Прервать И Или Не Знач Асинх Ждать Procedure Function EndProcedure EndFunction Export If Then Else ElsIf EndIf For Each In To Do EndDo While Return Var New True False Undefined Try Except EndTry Raise Continue Break And Or Not Val Async Await'.toLowerCase().split(' '));
  let html = '', end = 0;
  for (const match of line.matchAll(pattern)) {html += escapeHtml(line.slice(end, match.index)); const t = match[0]; const cls = t.startsWith('//') ? 'comment' : t.startsWith('"') ? 'string' : /^\d/.test(t) ? 'number' : keywords.has(t.toLowerCase()) ? 'keyword' : /^\s*\(/.test(line.slice(match.index+t.length)) ? 'function-token' : ''; html += cls ? `<span class="${cls}">${escapeHtml(t)}</span>` : escapeHtml(t); end = match.index + t.length;}
  return html + escapeHtml(line.slice(end));
}
function renderCode() {
  if (!file) return;
  const query = $('#code-search').value.toLocaleLowerCase('ru'); matches = []; matchIndex = -1;
  const bsl = /\.(bsl|os)$/i.test(file.path);
  // Build once per file/search, never inject unescaped project content.
  $('#source').innerHTML = (editDraft?.content ?? file.content).split(/\r?\n/).map((line, i) => {
    let html;
    if (query) {
      const lower = line.toLocaleLowerCase('ru'); let start = 0, found, chunks = [];
      while ((found = lower.indexOf(query, start)) >= 0) {chunks.push(escapeHtml(line.slice(start, found)), '<mark>', escapeHtml(line.slice(found, found + query.length)), '</mark>'); start = found + query.length; if (!matches.includes(i + 1)) matches.push(i + 1);}
      chunks.push(escapeHtml(line.slice(start))); html = chunks.join('');
    } else html = highlight(line, bsl);
    return `<span class="code-line" data-line="${i + 1}" id="line-${i+1}">${html || ' '}</span>`;
  }).join('');
  $('#match-count').textContent = query ? `${matches.length}` : '';
}
function jump(line) {document.querySelectorAll('.code-line.current').forEach(e => e.classList.remove('current')); const target = $('#line-' + line); if (target) {target.classList.add('current'); target.scrollIntoView({block:'center'});}}
let searchTimer;
$('#code-search').oninput = () => {clearTimeout(searchTimer); searchTimer = setTimeout(() => {renderCode(); if (matches.length) {matchIndex = 0; jump(matches[0]);}}, 180);};
$('#next-match').onclick = () => {if (matches.length) {matchIndex = (matchIndex + 1) % matches.length; jump(matches[matchIndex]);}};
$('#code-search').onkeydown = e => {if (e.key === 'Enter') $('#next-match').click();};
$('#copy').onclick = async () => {if (!file) return; try {await navigator.clipboard.writeText(editingMode && file.editable ? $('#code-editor').value : file.content); toast('Исходный код скопирован');} catch(e) {toast('Не удалось скопировать. Выделите нужный текст в просмотрщике.');}};
async function initialize() {
  try {const onboarding=await api('onboarding');if(!onboarding.completed){location.replace('/setup');return;}await refreshConnections(); const state = await api('state'); renderRecent(state.recent); if (state.job.status === 'running' || state.job.status === 'ready') {busy(true); try {if (state.job.status === 'running' && state.job.operation === 'export') {populateExport(connectionData.profiles.find(p => p.id === state.job.connection)); $('#export-dialog').showModal(); exportBusy(true);} await waitForLoad(); if ($('#export-dialog').open) $('#export-dialog').close();} finally {busy(false); exportBusy(false);}}}
  catch(e) {toast(e.message);}
}

async function refreshConnections() {
  connectionData = await api('connections');
  $('#refresh').textContent = connectionData.active ? '↻ Выгрузить снова' : '↻ Обновить';
}
function exportBusy(value) {
  $('#export-fields').disabled = value; $('#start-export').disabled = value;
  $('#cancel-export').hidden = !value; $('#cancel-export').disabled = false;
  $('#export-local-note').hidden = value;
}
function updateBaseKind() {
  const server = $('#base-kind').value === 'server';
  $('#base-location-label').textContent = server ? 'Адрес серверной базы' : 'Папка информационной базы';
  $('#base-location').placeholder = server ? 'server:1541/МояБаза' : 'C:\\Базы\\МояБаза';
  $('#base-hint').textContent = server ? 'Имя сервера и имя базы в кластере 1С: сервер[:порт]/имя_базы.' : 'Папка, в которой находится файл 1Cv8.1CD.';
  $('#choose-base').hidden = server;
}
function updateAuth() {
  const os = $('#os-auth').checked;
  $('#base-user').disabled = $('#base-password').disabled = os;
}
function populateExport(profile) {
  $('#connection-name').value = profile?.name || '';
  $('#base-kind').value = profile?.kind || 'file';
  $('#base-location').value = profile?.location || '';
  $('#platform-path').value = profile?.platform || connectionData.preferred_platform || connectionData.platforms[0] || '';
  $('#base-user').value = profile?.user || '';
  $('#base-password').value = '';
  $('#os-auth').checked = Boolean(profile?.os_auth);
  $('#extension-name').value = profile?.extension || '';
  $('#password-hint').textContent = profile?.has_password ? 'Пароль уже есть в памяти Athena. Оставьте поле пустым, чтобы использовать его, или введите новый.' : 'Пароль хранится только в памяти до остановки Athena. Настройки подключения сохраняются без пароля.';
  updateBaseKind(); updateAuth();
}
async function showExportDialog(profile = null) {
  try {
    await refreshConnections();
    const select = $('#connection'); select.replaceChildren();
    function option(parent, value, label) {const item = document.createElement('option'); item.value = value; item.textContent = label; parent.append(item);}
    option(select, '', 'Новое подключение');
    if (connectionData.profiles.length) {const group = document.createElement('optgroup'); group.label = 'Сохранённые подключения'; connectionData.profiles.forEach(p => option(group, 'profile:' + p.id, p.name)); select.append(group);}
    if (connectionData.bases.length) {const group = document.createElement('optgroup'); group.label = 'Базы из списка запуска 1С'; connectionData.bases.forEach((p,i) => option(group, 'base:' + i, p.name)); select.append(group);}
    $('#platform-options').replaceChildren(); connectionData.platforms.forEach(path => option($('#platform-options'), path, path));
    $('#platform-hint').textContent = connectionData.platforms.length ? 'Выберите версию, подходящую вашей базе.' : 'Платформа не найдена в стандартных каталогах. Укажите путь или выберите файл через «Обзор…».';
    const preferred = profile || connectionData.profiles.find(p => p.id === connectionData.active) || connectionData.profiles[0];
    select.value = preferred ? 'profile:' + preferred.id : '';
    populateExport(preferred); exportBusy(false);
    $('#export-error').textContent = ''; $('#export-progress').hidden = true; $('#export-log').hidden = true;
    if (!$('#export-dialog').open) $('#export-dialog').showModal();
  } catch(e) {toast(e.message);}
}
$('#export-project').onclick = () => showExportDialog();
$('#close-export').onclick = () => $('#export-dialog').close();
$('#base-kind').onchange = updateBaseKind;
$('#os-auth').onchange = updateAuth;
$('#connection').onchange = () => {
  const value = $('#connection').value;
  const profile = value.startsWith('profile:') ? connectionData.profiles.find(p => p.id === value.slice(8)) : value.startsWith('base:') ? connectionData.bases[Number(value.slice(5))] : null;
  populateExport(profile);
};
for (const [button, field, purpose] of [['#choose-base','#base-location','base'], ['#choose-platform','#platform-path','platform']]) {
  $(button).onclick = async () => {$(button).disabled = true; try {const result = await api('choose', {purpose}); if (result.path) $(field).value = result.path;} catch(e) {$('#export-error').textContent = e.message;} finally {$(button).disabled = false;}};
}
function exportValues() {
  return {name: $('#connection-name').value, kind: $('#base-kind').value, location: $('#base-location').value,
          platform: $('#platform-path').value, user: $('#base-user').value, password: $('#base-password').value,
          os_auth: $('#os-auth').checked, extension: $('#extension-name').value};
}
$('#export-form').onsubmit = e => {e.preventDefault(); startExport(exportValues());};
async function startExport(values) {
  if (loading) return;
  busy(true); exportBusy(true); $('#export-error').textContent = ''; $('#export-log').hidden = true;
  $('#export-progress').textContent = 'Запуск выгрузки…'; $('#export-progress').hidden = false;
  try {
    await api('export', values); $('#base-password').value = '';
    await waitForLoad(); $('#export-dialog').close(); toast('Конфигурация выгружена из 1С и открыта');
  } catch(e) {$('#export-error').textContent = e.message; $('#status').textContent = 'Выгрузка не завершена'; if (!$('#export-dialog').open) $('#export-dialog').showModal();}
  finally {busy(false); exportBusy(false);}
}
$('#cancel-export').onclick = async () => {$('#cancel-export').disabled = true; try {await api('cancel', {}); $('#export-progress').textContent = 'Отмена выгрузки…';} catch(e) {toast(e.message); $('#cancel-export').disabled = false;}};


// The graph uses bounded neighborhoods, so large configurations stay interactive.
let graphGroup = null, graphPage = 0, graphPoints = [], graphEdges = [];
let graphCamera = {x:0,y:0,k:1}, graphDrag = null;
const graphSize = 8, svgNS = 'http://www.w3.org/2000/svg';
function graphElement(tag, attrs, text) {
  const element = document.createElementNS(svgNS, tag);
  for (const [key,value] of Object.entries(attrs || {})) element.setAttribute(key,value);
  if (text !== undefined) element.textContent = text;
  return element;
}
function graphGroups(node) {
  const groups = new Map();
  for (const id of node.children) {
    const child = nodes.get(id); if (!child) continue;
    if (!groups.has(child.kind)) groups.set(child.kind, {kind:child.kind,label:child.label,members:[]});
    groups.get(child.kind).members.push(child);
  }
  return [...groups.values()].sort((a,b) => a.label.localeCompare(b.label,'ru'));
}
function renderGraph() {
  const focus = nodes.get(selected); if (!focus) return;
  const groups = graphGroups(focus);
  const group = groups.find(g => g.kind === graphGroup);
  const entries = group ? group.members : groups;
  const pages = Math.max(1, Math.ceil(entries.length / graphSize));
  graphPage = Math.min(graphPage,pages-1);
  const visible = entries.slice(graphPage*graphSize,(graphPage+1)*graphSize);
  const world = $('#graph-world'); world.replaceChildren();
  $('#graph-detail').hidden = true;
  graphPoints = []; graphEdges = [];
  $('#graph-scope').textContent = group ? `${focus.name} / ${group.label} · ${group.members.length} элементов` : `${focus.name} · ${focus.children.length} элементов в составе`;
  $('#graph-up').disabled = !group && !focus.parent;
  $('#graph-prev').disabled = graphPage === 0; $('#graph-next').disabled = graphPage === pages-1;
  $('#graph-page').textContent = `${graphPage+1} / ${pages}`;
  function point(key,label,subtitle,x,y,node,virtual) {
    const color = virtual ? '#e7bb70' : node?.kind === 'Module' ? '#76c6ae' : '#ad96ff';
    const p = {key,x,y,node,virtual,label,subtitle,color}; graphPoints.push(p); return p;
  }
  const center = point(focus.id,focus.name,focus.label,0,0,focus,null);
  const parent = nodes.get(focus.parent);
  if (parent) {const p=point(parent.id,parent.name,'↑ '+parent.label,-340,0,parent,null); graphEdges.push([p,center]);}
  let source = center;
  if (group) {
    source=point('group:'+group.kind,group.label,`${group.members.length} элементов`,320,0,null,group);
    graphEdges.push([center,source]);
  }
  visible.forEach((entry,index) => {
    const y=(index-(visible.length-1)/2)*86;
    if (group) {
      const p=point(entry.id,entry.name,entry.label,640,y,entry,null); graphEdges.push([source,p]);
    } else {
      const p=point('group:'+entry.kind,entry.label,`${entry.members.length} элементов · раскрыть`,320,y,null,entry); graphEdges.push([center,p]);

    }
  });
  const edgeLayer=graphElement('g',{'class':'graph-edges'}); world.append(edgeLayer);
  for (const edge of graphEdges) {
    edge.element=graphElement('path',{'class':'graph-link'}); edgeLayer.append(edge.element);
  }
  for (const p of graphPoints) {
    const g=graphElement('g',{'class':'graph-node'+(p.key===focus.id?' graph-focus':''),role:'button',tabindex:0,'aria-label':`${p.subtitle}: ${p.label}`,'data-key':p.key});
    p.element=g;
    g.append(graphElement('rect',{x:-126,y:-29,width:252,height:58,rx:12,stroke:p.color}));
    g.append(graphElement('circle',{cx:-109,cy:-10,r:3,fill:p.color}));
    const name=p.label.length>27?p.label.slice(0,26)+'…':p.label;
    g.append(graphElement('text',{x:-98,y:-6,'class':'graph-node-name'},name));
    g.append(graphElement('text',{x:-109,y:15,'class':'graph-node-subtitle'},p.subtitle.length>35?p.subtitle.slice(0,34)+'…':p.subtitle));
    g.append(graphElement('title',{},p.label+' · '+p.subtitle));
    g.onclick=() => {if (!g.dataset.moved) showGraphDetail(p);};
    g.ondblclick=() => openGraphPoint(p);
    g.onkeydown=e => {if(e.key==='Enter') {e.preventDefault();showGraphDetail(p);} if(e.key===' ') {e.preventDefault();openGraphPoint(p);}};
    g.onpointerdown=e => {
      if (e.button!==0) return; e.stopPropagation();
      g.dataset.moved=''; graphDrag={point:p,startX:e.clientX,startY:e.clientY,x:p.x,y:p.y};
      g.setPointerCapture(e.pointerId);
    };
    world.append(g);
  }
  updateGraphPositions(); fitGraph();
  $('#graph-count').textContent = `${graphPoints.length} узлов на экране · ${nodes.size} в проекте`;
}
function openGraphPoint(p) {
  if (p.virtual) {graphGroup=p.virtual.kind;graphPage=0;renderGraph();}
  else if (p.node) selectNode(p.node.id,'graph');
}
function showGraphDetail(p) {
  const panel=$('#graph-detail');panel.replaceChildren();panel.hidden=false;
  const close=document.createElement('button');close.className='graph-detail-close';close.textContent='×';close.setAttribute('aria-label','Закрыть карточку узла');close.onclick=()=>panel.hidden=true;panel.append(close);
  const label=document.createElement('small');label.textContent=p.virtual?'ГРУППА МЕТАДАННЫХ':p.node.label;panel.append(label);
  const title=document.createElement('strong');title.textContent=p.label;panel.append(title);
  const info=document.createElement('p');info.textContent=p.virtual ? `${p.virtual.members.length} объектов этого типа` : `${p.node.children.length} элементов в составе · ${p.node.files.length} исходных файлов`;panel.append(info);
  const expand=document.createElement('button');expand.className='primary';expand.textContent=p.virtual?'Раскрыть группу':'Показать связи объекта';expand.onclick=()=>openGraphPoint(p);panel.append(expand);
  if(p.node) {
    const overview=document.createElement('button');overview.textContent='Открыть свойства';overview.onclick=()=>selectNode(p.node.id,'overview');panel.append(overview);
    const code=document.createElement('button');code.textContent='Открыть код и XML';code.onclick=()=>selectNode(p.node.id,'code');panel.append(code);
  }
  document.querySelectorAll('.graph-node').forEach(g=>g.classList.toggle('graph-picked',g===p.element));
}
function updateGraphPositions() {
  for (const p of graphPoints) if(p.element) p.element.setAttribute('transform',`translate(${p.x} ${p.y})`);
  for (const edge of graphEdges) {
    const [a,b]=edge, x1=a.x+126,x2=b.x-126,m=(x1+x2)/2;
    edge.element.setAttribute('d',`M ${x1} ${a.y} C ${m} ${a.y}, ${m} ${b.y}, ${x2} ${b.y}`);
  }
}
function updateGraphCamera() {$('#graph-world').setAttribute('transform',`translate(${graphCamera.x} ${graphCamera.y}) scale(${graphCamera.k})`);}
function fitGraph() {
  if(!graphPoints.length || activeTab!=='graph')return;
  const svg=$('#graph-svg'),width=svg.clientWidth,height=svg.clientHeight;
  if(!width||!height)return;
  const minX=Math.min(...graphPoints.map(p=>p.x))-150,maxX=Math.max(...graphPoints.map(p=>p.x))+150;
  const minY=Math.min(...graphPoints.map(p=>p.y))-65,maxY=Math.max(...graphPoints.map(p=>p.y))+65;
  const k=Math.min(1.15,(width-64)/(maxX-minX),(height-70)/(maxY-minY));
  graphCamera={k,x:width/2-(minX+maxX)*k/2,y:height/2-(minY+maxY)*k/2};updateGraphCamera();
}
function zoomGraph(factor,x,y) {
  const svg=$('#graph-svg');x??=svg.clientWidth/2;y??=svg.clientHeight/2;
  const k=Math.max(.08,Math.min(3,graphCamera.k*factor)), ratio=k/graphCamera.k;
  graphCamera={k,x:x-(x-graphCamera.x)*ratio,y:y-(y-graphCamera.y)*ratio};updateGraphCamera();
}
$('#graph-fit').onclick=fitGraph;
$('#graph-tree').onclick=()=>$('#tree-search').focus();
$('#graph-plus').onclick=()=>zoomGraph(1.25);$('#graph-minus').onclick=()=>zoomGraph(.8);
$('#graph-root').onclick=()=>selectNode(project.configuration,'graph');
$('#graph-up').onclick=()=> {if(graphGroup){graphGroup=null;graphPage=0;renderGraph();}else{const parent=nodes.get(selected)?.parent;if(parent)selectNode(parent,'graph');}};
$('#graph-prev').onclick=()=>{graphPage--;renderGraph();};$('#graph-next').onclick=()=>{graphPage++;renderGraph();};
$('#graph-svg').onwheel=e=>{e.preventDefault();const rect=e.currentTarget.getBoundingClientRect();zoomGraph(Math.exp(-e.deltaY*.0015),e.clientX-rect.left,e.clientY-rect.top);};
$('#graph-svg').onpointerdown=e=> {if(e.button!==0)return;graphDrag={startX:e.clientX,startY:e.clientY,x:graphCamera.x,y:graphCamera.y};e.currentTarget.setPointerCapture(e.pointerId);};
$('#graph-svg').onpointermove=e=> {
  if(!graphDrag)return;const dx=e.clientX-graphDrag.startX,dy=e.clientY-graphDrag.startY;
  if(graphDrag.point){const p=graphDrag.point;if(Math.abs(dx)+Math.abs(dy)>4)p.element.dataset.moved='1';p.x=graphDrag.x+dx/graphCamera.k;p.y=graphDrag.y+dy/graphCamera.k;updateGraphPositions();}
  else{graphCamera.x=graphDrag.x+dx;graphCamera.y=graphDrag.y+dy;updateGraphCamera();}
};
$('#graph-svg').onpointerup=$('#graph-svg').onpointercancel=()=>graphDrag=null;
$('#graph-svg').onlostpointercapture=()=>graphDrag=null;
new ResizeObserver(()=>{if(activeTab==='graph')fitGraph();}).observe($('#graph-svg'));
$('#graph-search').oninput=()=> {
  const query=$('#graph-search').value.trim().toLocaleLowerCase('ru'),results=$('#graph-results');results.replaceChildren();results.hidden=!query;
  if(!query||!project)return;
  const found=[];
  for(const node of nodes.values()) {
    const name=node.name.toLocaleLowerCase('ru');
    if(!`${name} ${node.label} ${node.properties.Synonym||''}`.toLocaleLowerCase('ru').includes(query))continue;
    const score=(name===query?0:name.startsWith(query)?20:name.includes(query)?40:80)+(node.parent===project.configuration?0:10);
    found.push({node,score});
  }
  found.sort((a,b)=>a.score-b.score || a.node.name.localeCompare(b.node.name,'ru'));
  for(const {node} of found.slice(0,20)) {
    const button=document.createElement('button');button.textContent=`${node.label} · ${node.name}`;
    const parent=nodes.get(node.parent);if(parent && parent.id!==project.configuration){const context=document.createElement('small');context.textContent=parent.name;button.append(context);}
    button.onclick=()=>{results.hidden=true;$('#graph-search').value='';selectNode(node.id,'graph');};results.append(button);
  }
  if(!found.length)results.textContent='Узлы не найдены';
  else if(found.length>20){const hint=document.createElement('small');hint.textContent=`Первые 20 из ${found.length} совпадений. Уточните запрос.`;results.append(hint);}
};
$('#graph-search').onkeydown=e=>{if(e.key==='Escape')$('#graph-results').hidden=true;if(e.key==='Enter')$('#graph-results button')?.click();};

function cleanCode(text) {return text.replace(/\r\n?/g,'\n');}
function draftKey() {return `athena-draft:${$('meta[name=athena-user]')?.content||'legacy'}:${connectionData.active || project.root}:${file.path}`;}
function restoreDraft() {
  if(!file?.editable)return;
  const key=draftKey();let stored=editorDrafts.get(key);
  if(!stored)try {stored=JSON.parse(localStorage.getItem(key));}catch(e){}
  if(stored && typeof stored.content==='string' && cleanCode(stored.content)===cleanCode(file.content)){
    editorDrafts.delete(key);try{localStorage.removeItem(key);}catch(e){}stored=null;
  }
  if(stored && typeof stored.content==='string' && typeof stored.baseHash==='string' && typeof stored.original==='string')editDraft=stored;
  else editDraft={content:cleanCode(file.content),original:cleanCode(file.content),baseHash:file.hash,key};
  editDraft.key=key;$('#code-editor').value=editDraft.content;
}
function hasDraft() {return !!editDraft && cleanCode(editDraft.content)!==cleanCode(editDraft.original);}
function editorControls() {
  const editable=!!file?.editable;
  const edit=editable && editingMode;
  $('.workspace').classList.toggle('code-edit-mode',edit && activeTab==='code');
  $('#edit-tree').hidden=!edit;
  $('#edit-mode').disabled=!editable;$('#edit-mode').textContent=edit?'Просмотр исходника':'Редактировать BSL';
  $('#save-draft').disabled=!editable || !hasDraft();
  $('#preview-upload').disabled=!editable || !hasDraft() || uploading || editDraft.baseHash!==file.hash;
  $('#discard-draft').hidden=!hasDraft();
  $('#undo-discard').hidden=!editDraft || hasDraft() || !discardedDrafts.has(editDraft.key);
  $('#source').hidden=edit;$('#editing-surface').hidden=!edit;
  $('.editor-layout').classList.toggle('editing',edit);
  $('#object-mode-tag').textContent=edit?(hasDraft()?'Локальный черновик':'Редактор BSL'):'Просмотр';
  $('#draft-status').textContent=!editable?'XML доступен для просмотра':hasDraft()?'Есть локальные изменения':'Модуль без локальных изменений';
  $('#draft-conflict').hidden=!hasDraft() || editDraft.baseHash===file.hash;
  $('#code-search').disabled=$('#next-match').disabled=edit;
  $('#source-language').textContent=editable?'BSL':'XML';
  if(edit)renderEditorPaint();else if(file)renderCode();
  updateEditorPosition();renderChanges();
}
function saveEditorDraft(notify=false) {
  if(!editDraft)return;
  editDraft.content=$('#code-editor').value;
  editorDrafts.set(editDraft.key,{...editDraft});
  try {localStorage.setItem(editDraft.key,JSON.stringify(editDraft));if(notify)toast('Черновик сохранён локально. В 1С изменения ещё не загружены.');}
  catch(e){toast('Не удалось сохранить черновик в браузере. Текст остаётся в памяти; подготовьте загрузку, чтобы сохранить копию на диске.');}
  preparedUpload=null;editorControls();renderDocuments();renderChanges();
}
$('#edit-tree').onclick=()=>$('#tree-search').focus();
$('#edit-mode').onclick=()=>{editingMode=!editingMode;editorControls();if(editingMode)$('#code-editor').focus();};
$('#code-editor').oninput=()=>saveEditorDraft();
$('#code-editor').onkeydown=e=>{
  if(e.key==='Tab'){e.preventDefault();const input=e.currentTarget,start=input.selectionStart,end=input.selectionEnd;input.setRangeText('\t',start,end,'end');saveEditorDraft();}
  if((e.ctrlKey||e.metaKey)&&e.key.toLowerCase()==='s'){e.preventDefault();saveEditorDraft(true);}
};
$('#save-draft').onclick=()=>saveEditorDraft(true);
$('#discard-draft').onclick=()=>{
  if(!editDraft)return;
  discardedDrafts.set(editDraft.key,{...editDraft});
  editorDrafts.delete(editDraft.key);try{localStorage.removeItem(editDraft.key);}catch(e){}
  editDraft={key:editDraft.key,content:cleanCode(file.content),original:cleanCode(file.content),baseHash:file.hash};
  $('#code-editor').value=editDraft.content;preparedUpload=null;editorControls();
};
$('#undo-discard').onclick=()=>{
  if(!editDraft)return;const saved=discardedDrafts.get(editDraft.key);if(!saved)return;
  editDraft={...saved};$('#code-editor').value=editDraft.content;saveEditorDraft();discardedDrafts.delete(editDraft.key);editorControls();
};
function renderDifference(text) {
  const pre=$('#upload-diff');pre.replaceChildren();
  for(const line of text.split('\n')){const span=document.createElement('span');span.className=line.startsWith('+')?'diff-add':line.startsWith('-')?'diff-remove':line.startsWith('@@')?'diff-context':'';span.textContent=line+'\n';pre.append(span);}
}
function uploadDialogReset() {
  $('#upload-error').textContent='';$('#upload-status').hidden=true;$('#upload-log').hidden=true;
  $('#upload-password').value='';$('#confirm-upload').disabled=false;$('#close-upload').disabled=false;
}
$('#preview-upload').onclick=async()=>{
  if(!editDraft || !hasDraft())return;
  $('#preview-upload').disabled=true;
  try{
    await refreshConnections();
    const result=await api('prepare-change',{file_id:currentFileId,content:editDraft.content,base_hash:editDraft.baseHash});
    preparedUpload={...result,key:editDraft.key,content:editDraft.content};rebaseMode=false;
    uploadDialogReset();$('#upload-heading').textContent='Загрузка модуля в 1С';
    $('#upload-target').textContent=`Подключение: ${result.connection}${result.extension?` · Расширение: ${result.extension}`:' · Основная конфигурация'}`;
    $('#upload-path').textContent=result.path;renderDifference(result.diff);
    $('#upload-backup').textContent=`Исходный текст сохранён: ${result.backup}${result.truncated?' · Большая разница показана частично.':''}`;
    const profile=connectionData.profiles.find(p=>p.id===connectionData.active);
    $('#upload-password-row').hidden=!profile?.user || profile.os_auth;
    $('#upload-password').placeholder=profile?.has_password?'Оставьте пустым, чтобы использовать пароль из памяти':'Пароль пользователя 1С';
    $('#confirm-upload').hidden=false;$('#accept-rebase').hidden=true;$('#upload-notice').hidden=false;
    $('#upload-dialog').showModal();
  }catch(e){toast(e.message);}finally{editorControls();}
};
$('#close-upload').onclick=()=>{if(!uploading)$('#upload-dialog').close();};
$('#upload-dialog').oncancel=e=>{if(uploading)e.preventDefault();};
$('#confirm-upload').onclick=async()=>{
  if(uploading || !preparedUpload || rebaseMode)return;
  const submitted=preparedUpload;uploading=true;busy(true);$('#confirm-upload').disabled=true;$('#close-upload').disabled=true;
  $('#upload-error').textContent='';$('#upload-status').hidden=false;$('#upload-status').textContent='Проверка сохранённых изменений в 1С…';
  try{
    await api('apply-change',{id:submitted.id,password:$('#upload-password').value});$('#upload-password').value='';
    while(true){
      const state=await api('state'),job=state.job;
      $('#upload-status').textContent=job.phase||'Загрузка…';
      if(job.log){$('#upload-log').hidden=false;$('#upload-log-text').textContent=job.log;}
      if(job.status==='error')throw Error(job.error);
      if(job.status==='ready'){
        if(submitted.changeSet){$('#upload-status').textContent='Набор загружен и проверен повторной выгрузкой. Обновите список наборов в разделе «Правки». ';}
        if(submitted.key){editorDrafts.delete(submitted.key);try{localStorage.removeItem(submitted.key);}catch(e){}}
        await refreshConnections();await updateProject(state,true);
        if(submitted.object){
          try{localStorage.removeItem(objectDraftKey());}catch(e){}
          const created=[...nodes.values()].find(n=>n.kind===submitted.spec.kind && n.name===submitted.spec.name);
          if(created)selectNode(created.id,'graph');
          preparedObject=null;$('#object-preview').hidden=true;$('#upload-object').hidden=true;
        }
        $('#upload-status').textContent=submitted.changeSet?'Набор загружен и проверен повторной выгрузкой.':submitted.object?'Объект создан в 1С и проверен повторной выгрузкой.':'Модуль загружен в 1С и проверен повторной выгрузкой.';
        $('#confirm-upload').hidden=true;toast(submitted.changeSet?'Набор загружен и проверен':submitted.object?'Объект создан и проверен':'Модуль загружен и проверен');break;
      }
      await new Promise(resolve=>setTimeout(resolve,500));
    }
  }catch(e){$('#upload-error').textContent=e.message;$('#upload-status').textContent='Загрузка требует внимания';$('#confirm-upload').hidden=true;}
  finally{uploading=false;busy(false);$('#close-upload').disabled=false;editorControls();}
};
$('#rebase-draft').onclick=()=>{
  if(!editDraft || !file)return;rebaseMode=true;preparedUpload=null;uploadDialogReset();
  $('#upload-heading').textContent='Сравнение с текущим исходником';
  $('#upload-target').textContent='Слева — текущая версия из 1С. Справа — ваш сохранённый черновик. Принятие основы не загружает код в 1С.';
  $('#upload-path').textContent=file.path;
  renderDifference('--- Текущий исходник\n'+cleanCode(file.content).split('\n').map(s=>'- '+s).join('\n')+'\n+++ Черновик\n'+editDraft.content.split('\n').map(s=>'+ '+s).join('\n'));
  $('#upload-backup').textContent='После сравнения подготовьте загрузку заново.';
  $('#upload-password-row').hidden=true;$('#confirm-upload').hidden=true;$('#accept-rebase').hidden=false;$('#upload-notice').hidden=true;
  $('#upload-dialog').showModal();
};
$('#accept-rebase').onclick=()=>{
  if(!rebaseMode || !editDraft || !file)return;
  editDraft.original=cleanCode(file.content);editDraft.baseHash=file.hash;saveEditorDraft();$('#upload-dialog').close();editorControls();
};

let preparedObject=null, objectPreparing=false;
function objectDraftKey(){return `athena-new-object:${connectionData.active || project?.root}`;}
function objectTypeOptions(){
  return [{value:'String',label:'Строка'},{value:'Number',label:'Число'},{value:'Boolean',label:'Булево'},{value:'Date',label:'Дата'},
    ...[...nodes.values()].filter(n=>['Catalog','Document'].includes(n.kind)).map(n=>({value:`${n.kind}Ref.${n.name}`,label:`${n.label}: ${n.name}`}))];
}
function attributeRow(parent,data={}){
  const row=document.createElement('div');row.className='attribute-row';
  row.innerHTML='<input data-field="name" placeholder="Имя реквизита" maxlength="80" required aria-label="Имя реквизита"><input data-field="synonym" placeholder="Синоним" maxlength="500" aria-label="Синоним реквизита"><select data-field="type" aria-label="Тип реквизита"></select><input data-field="length" type="number" min="0" max="4096" value="100" aria-label="Длина строки" title="Длина строки; 0 — неограниченная"><input data-field="digits" type="number" min="1" max="38" value="15" aria-label="Разрядность числа" title="Разрядность"><input data-field="scale" type="number" min="0" max="38" value="2" aria-label="Дробная часть числа" title="Дробная часть"><button type="button" class="remove-field" aria-label="Удалить реквизит">×</button>';
  const select=row.querySelector('[data-field="type"]');
  for(const [key,title] of Object.entries({length:'Длина',digits:'Разрядность',scale:'Точность'})){
    const input=row.querySelector(`[data-field="${key}"]`),label=document.createElement('label');label.className='type-parameter';label.textContent=title;input.replaceWith(label);label.append(input);
  }
  for(const option of objectTypeOptions()){const o=document.createElement('option');o.value=option.value;o.textContent=option.label;select.append(o);}
  for(const [key,value] of Object.entries(data)){const input=row.querySelector(`[data-field="${key}"]`);if(input)input.value=value;}
  const controls=()=>{for(const key of ['length','digits','scale']){const input=row.querySelector(`[data-field="${key}"]`);input.parentElement.hidden=input.hidden=input.disabled=key==='length'?select.value!=='String':select.value!=='Number';}};
  select.onchange=()=>{controls();objectChanged();};controls();row.querySelector('button').onclick=()=>{row.remove();objectChanged();};parent.append(row);
}
function sectionRow(data={}){
  const section=document.createElement('section');section.className='section-row';
  section.innerHTML='<div class="section-heading"><input data-section="name" placeholder="Имя табличной части" maxlength="80" required aria-label="Имя табличной части"><input data-section="synonym" placeholder="Синоним" maxlength="500" aria-label="Синоним табличной части"><button type="button" class="remove-section" aria-label="Удалить табличную часть">×</button></div><div class="section-attributes"></div><button type="button" class="add-column">＋ Колонка</button>';
  for(const key of ['name','synonym'])section.querySelector(`[data-section="${key}"]`).value=data[key]||'';
  section.querySelector('.remove-section').onclick=()=>{section.remove();objectChanged();};
  section.querySelector('.add-column').onclick=()=>{attributeRow(section.querySelector('.section-attributes'));objectChanged();};
  for(const row of data.attributes||[])attributeRow(section.querySelector('.section-attributes'),row);
  $('#new-sections').append(section);
}
function objectValues(){
  const attributes=parent=>[...parent.children].filter(n=>n.classList.contains('attribute-row')).map(row=>{
    const result={};for(const input of row.querySelectorAll('[data-field]')){if(!input.disabled)result[input.dataset.field]=input.type==='number'?Number(input.value):input.value;}return result;
  });
  return {kind:$('#new-kind').value,name:$('#new-name').value,synonym:$('#new-synonym').value,attributes:attributes($('#new-attributes')),
    sections:[...$('#new-sections').children].map(s=>({name:s.querySelector('[data-section="name"]').value,synonym:s.querySelector('[data-section="synonym"]').value,attributes:attributes(s.querySelector('.section-attributes'))})),
    object_module:$('#new-object-module').value,manager_module:$('#new-manager-module').value};
}
function objectChanged(){
  preparedObject=null;$('#object-preview').hidden=true;$('#upload-object').hidden=true;$('#create-error').textContent='';
  try{localStorage.setItem(objectDraftKey(),JSON.stringify(objectValues()));$('#create-draft-status').textContent='Черновик сохранён в браузере';}
  catch(e){$('#create-draft-status').textContent='Черновик в памяти; подготовьте объект для сохранения на диск';}
}
$('#create-object').onclick=()=>openCreation();
async function openCreation(kind){
  if(!project||loading)return;if(creationOpen && (!kind||kind===$('#new-kind').value)){showCreation();return;}await refreshConnections();
  let data={};try{data=JSON.parse(localStorage.getItem(objectDraftKey()))||{};}catch(e){}
  $('#new-kind').value=kind||data.kind||'Catalog';$('#new-name').value=data.name||'';$('#new-synonym').value=data.synonym||'';
  $('#new-object-module').value=data.object_module||'';$('#new-manager-module').value=data.manager_module||'';
  $('#new-attributes').replaceChildren();$('#new-sections').replaceChildren();
  for(const row of data.attributes||[])attributeRow($('#new-attributes'),row);for(const s of data.sections||[])sectionRow(s);
  const profile=connectionData.profiles.find(p=>p.id===connectionData.active);
  $('#create-target').textContent=profile?`Цель: ${profile.name} · ${profile.extension?`расширение ${profile.extension}`:'основная конфигурация'}`:'Локальный проект: пакет можно подготовить и сохранить. Для отправки откройте выгрузку, связанную с подключением 1С.';
  $('#object-preview').hidden=true;$('#upload-object').hidden=true;preparedObject=null;$('#create-error').textContent='';showCreation();$('#new-name').focus();
}
$('#close-create').onclick=()=>{if(!objectPreparing)closeCreation(true);};
$('#create-dialog').oncancel=e=>{if(objectPreparing)e.preventDefault();};
$('#create-form').oninput=objectChanged;
$('#new-kind').onchange=objectChanged;
$('#add-attribute').onclick=()=>{attributeRow($('#new-attributes'));objectChanged();};
$('#add-section').onclick=()=>{sectionRow();objectChanged();};
$('#create-form').onsubmit=async e=>{
  e.preventDefault();if(objectPreparing)return;objectPreparing=true;$('#prepare-object').disabled=true;$('#close-create').disabled=true;$('#create-error').textContent='';
  $('#create-draft-status').textContent='Подготовка метаданных…';
  try{
    const spec=objectValues(),root=project.root;
    const result=await api('prepare-object',spec);
    if(project.root!==root || JSON.stringify(objectValues())!==JSON.stringify(spec)){preparedObject=null;toast('Пакет сохранён на диск. Свойства изменились во время подготовки; подготовьте актуальный вариант снова.');return;}
    preparedObject=result;$('#object-preview').hidden=false;
    $('#object-package-path').textContent=`Пакет сохранён: ${preparedObject.folder}`;
    if(preparedObject.truncated)$('#object-package-path').textContent+=' · Большая разница показана частично; полный текст находится в файлах пакета.';
    $('#object-package-files').replaceChildren();for(const path of preparedObject.selected){const li=document.createElement('li');li.textContent=path;$('#object-package-files').append(li);}
    $('#object-package-diff').textContent=preparedObject.diff;
    $('#create-draft-status').textContent='Метаданные и код сохранены на диск; в 1С объект ещё не создан';
    $('#upload-object').hidden=!preparedObject.can_upload;
  }catch(e){$('#create-error').textContent=e.message;$('#create-draft-status').textContent='Проверьте свойства объекта';}
  finally{objectPreparing=false;$('#prepare-object').disabled=false;$('#close-create').disabled=false;}
};
$('#upload-object').onclick=()=>{
  if(!preparedObject)return;preparedUpload={...preparedObject,object:true};rebaseMode=false;uploadDialogReset();
  $('#upload-heading').textContent='Создание объекта в 1С';$('#upload-target').textContent=`Подключение: ${preparedObject.connection}${preparedObject.extension?' · Расширение: '+preparedObject.extension:''}`;
  $('#upload-path').textContent=preparedObject.path;renderDifference(preparedObject.diff);
  $('#upload-backup').textContent=`Исходный Configuration.xml сохранён: ${preparedObject.backup}`;
  const profile=connectionData.profiles.find(p=>p.id===connectionData.active);
  $('#upload-password-row').hidden=!profile?.user||profile.os_auth;$('#upload-password').placeholder=profile?.has_password?'Можно использовать пароль из памяти':'Пароль пользователя 1С';
  $('#confirm-upload').hidden=false;$('#accept-rebase').hidden=true;$('#upload-notice').hidden=false;
  closeCreation(true);$('#upload-dialog').showModal();
};
for(const id of ['#new-object-module','#new-manager-module'])$(id).onkeydown=e=>{
  if(e.key==='Tab'){e.preventDefault();const input=e.currentTarget;input.setRangeText('\t',input.selectionStart,input.selectionEnd,'end');objectChanged();}
  if((e.ctrlKey||e.metaKey)&&e.key.toLowerCase()==='s'){e.preventDefault();objectChanged();toast('Черновик объекта сохранён в браузере');}
};

const metadataOrder=['Subsystem','CommonModule','SessionParameter','Role','CommonAttribute','CommonForm','CommonCommand','CommonTemplate','CommonPicture','ExchangePlan','Constant','Catalog','Document','DocumentJournal','Enum','Report','DataProcessor','ChartOfCharacteristicTypes','ChartOfAccounts','ChartOfCalculationTypes','InformationRegister','AccumulationRegister','AccountingRegister','CalculationRegister','BusinessProcess','Task','ExternalDataSource','Module','Attribute','TabularSection','Form'];
const propertyLabels={Name:'Имя',Synonym:'Синоним',Comment:'Комментарий',Version:'Версия',CompatibilityMode:'Режим совместимости',DefaultRunMode:'Режим запуска',Hierarchical:'Иерархический',CodeLength:'Длина кода',DescriptionLength:'Длина наименования',Type:'Тип значения',Posting:'Проведение',NumberLength:'Длина номера',NumberType:'Тип номера',Autonumbering:'Автонумерация',CheckUnique:'Контроль уникальности',DataLockControlMode:'Управление блокировками'};
function subsystemObjects(node){const references=new Set((node.properties.Content||'').match(/[A-Za-z]+\.[А-Яа-яЁёA-Za-z_][А-Яа-яЁёA-Za-z_0-9]*/g)||[]);return [...references].map(ref=>rootObjects.get(ref)).filter(Boolean);}
$('#tree-mode').onchange=()=>renderTree(true);
function metadataRank(kind){const index=metadataOrder.indexOf(kind);return index<0?100:index;}
function renderModuleTabs(){const bar=$('#module-tabs');bar.replaceChildren();for(const f of availableFiles.filter(f=>f.language==='bsl')){const button=document.createElement('button');const parts=f.path.split('/');const base=parts.at(-1);const labels={ObjectModule:'Модуль объекта',ManagerModule:'Модуль менеджера',RecordSetModule:'Модуль набора записей',Module:'Модуль'};button.textContent=labels[base.replace(/\.bsl$/i,'')]||base;if(f.path.includes('/Forms/'))button.textContent='Форма: '+parts[parts.indexOf('Forms')+1];button.title=f.path;button.className=f.id===currentFileId?'active':'';button.onclick=()=>{$('#files').value=f.id;loadFile();};bar.append(button);}}
const groupLabels={Subsystem:'Подсистемы',CommonModule:'Общие модули',Catalog:'Справочники',Document:'Документы',Constant:'Константы',Enum:'Перечисления',Report:'Отчёты',DataProcessor:'Обработки',InformationRegister:'Регистры сведений',AccumulationRegister:'Регистры накопления',Role:'Роли',Attribute:'Реквизиты',TabularSection:'Табличные части',Form:'Формы',Module:'Модули',DocumentJournal:'Журналы документов'};
function rememberDocument(){const d=openDocuments.find(d=>d.id===selected);if(d){d.tab=activeTab;d.file=currentFileId;}}
function renderDocuments(){
  const bar=$('#document-tabs');bar.replaceChildren();openDocuments=openDocuments.filter(d=>nodes.has(d.id));
  for(const d of openDocuments){const row=document.createElement('div');row.className='document-tab'+(d.id===selected&&!creationVisible?' active':'');const b=document.createElement('button');b.textContent=nodes.get(d.id).name;b.title=ancestors(nodes.get(d.id)).join(' / ');b.onclick=async()=>{const saved={...d};selectNode(d.id,saved.tab);if(saved.tab==='code'&&saved.file&&availableFiles.some(f=>f.id===saved.file)){$('#files').value=saved.file;await loadFile();}};const close=document.createElement('button');close.textContent='×';close.setAttribute('aria-label','Закрыть вкладку '+nodes.get(d.id).name);close.onclick=()=>{openDocuments=openDocuments.filter(item=>item!==d);if(d.id===selected){if(openDocuments.length){const next=openDocuments.at(-1);selectNode(next.id,next.tab);}else{selected=null;$('#explorer').hidden=true;$('#welcome').hidden=false;renderInspector(null);}}renderDocuments();};row.append(b,close);bar.append(row);}
  if(creationOpen){const row=document.createElement('div');row.className='document-tab'+(creationVisible?' active':'');const b=document.createElement('button');b.textContent='＋ Новый объект';b.onclick=showCreation;const close=document.createElement('button');close.textContent='×';close.setAttribute('aria-label','Закрыть вкладку нового объекта');close.onclick=()=>{if(!objectPreparing)closeCreation(true);};row.append(b,close);bar.append(row);}
}
function showCreation(){creationOpen=true;creationVisible=true;$('#create-dialog').hidden=false;$('#explorer').hidden=true;$('#welcome').hidden=true;renderDocuments();}
function closeCreation(remove=false){if(remove)creationOpen=false;creationVisible=false;$('#create-dialog').hidden=true;$('#explorer').hidden=!selected;$('#welcome').hidden=!!selected;renderDocuments();}
function renderInspector(node){const panel=$('#inspector-content');panel.replaceChildren();if(!node)return;const h=document.createElement('h3');h.textContent=node.name;panel.append(h);const small=document.createElement('p');small.className='muted';small.textContent=node.label;panel.append(small);for(const [key,value] of Object.entries(node.properties)){const field=document.createElement('div');field.className='inspector-field';const label=document.createElement('small');label.textContent=propertyLabels[key] || key;const text=document.createElement('div');text.textContent=value||'—';field.append(label,text);panel.append(field);}}
function renderChanges(){const panel=$('#changes-list');panel.replaceChildren();const prefix=`athena-draft:${$('meta[name=athena-user]')?.content||'legacy'}:${connectionData.active || project?.root}:`;const drafts=new Map(editorDrafts);try{for(let i=0;i<localStorage.length;i++){const key=localStorage.key(i);if(key.startsWith(prefix)){const value=JSON.parse(localStorage.getItem(key));if(value)drafts.set(key,value);}}}catch(e){}let count=0;for(const [key,draft] of drafts){if(!key.startsWith(prefix)||cleanCode(draft.content)===cleanCode(draft.original))continue;count++;const path=key.slice(prefix.length),b=document.createElement('button');b.textContent=path;b.onclick=async()=>{const node=[...nodes.values()].find(n=>n.files.some(f=>f.path===path));if(!node){toast('Модуль отсутствует в текущей выгрузке. Черновик сохранён.');return;}const f=node.files.find(f=>f.path===path);selectNode(node.id,'code');$('#files').value=f.id;await loadFile();$('#changes-panel').hidden=true;};panel.append(b);}$('#changes-count').textContent=count?` ${count}`:'';if(!count){const p=document.createElement('p');p.className='muted';p.textContent='Нет изменённых модулей. Правки сохраняются локально при редактировании кода.';panel.append(p);}}
$('#rail-objects').onclick=()=>{$('#ai-panel').hidden=true;$('#changes-panel').hidden=true;$('#tree-search').focus();};
$('#rail-changes').onclick=()=>{$('#ai-panel').hidden=true;renderChanges();$('#changes-panel').hidden=!$('#changes-panel').hidden;};
$('#close-changes').onclick=()=>$('#changes-panel').hidden=true;
$('#toggle-inspector').onclick=()=>{const workspace=$('.workspace');if(window.matchMedia('(max-width:1250px)').matches)workspace.classList.toggle('inspector-overlay');else workspace.classList.toggle('inspector-hidden');};
$('#close-inspector').onclick=()=>{$('.workspace').classList.add('inspector-hidden');$('.workspace').classList.remove('inspector-overlay');};

let editorFontSize=14;
try{editorFontSize=Math.max(11,Math.min(22,Number(localStorage.getItem('athena-editor-font'))||14));}catch(e){}
function applyEditorFont(){document.documentElement.style.setProperty('--code-size',editorFontSize+'px');document.documentElement.style.setProperty('--code-leading',Math.round(editorFontSize*1.65)+'px');try{localStorage.setItem('athena-editor-font',editorFontSize);}catch(e){}syncEditorScroll();}
$('#font-smaller').onclick=()=>{editorFontSize=Math.max(11,editorFontSize-1);applyEditorFont();};
$('#font-larger').onclick=()=>{editorFontSize=Math.min(22,editorFontSize+1);applyEditorFont();};
function renderEditorPaint(){const text=$('#code-editor').value;$('#editor-paint').innerHTML=text.split('\n').map((line,i)=>`<span class="code-line" data-line="${i+1}">${highlight(line,true)||' '}</span>`).join('');syncEditorScroll();}
function syncEditorScroll(){const input=$('#code-editor'),paint=$('#editor-paint');paint.scrollTop=input.scrollTop;paint.scrollLeft=input.scrollLeft;}
function updateEditorPosition(){const input=$('#code-editor');const text=editingMode&&file?.editable?input.value:(editDraft?.content??file?.content??'');const before=text.slice(0,input.selectionStart);const row=before.split('\n').length,column=before.length-before.lastIndexOf('\n');$('#editor-position').textContent=editingMode&&file?.editable?`Строка ${row}, столбец ${column}`:'Просмотр исходника';$('#editor-lines').textContent=`${text.split('\n').length} строк`;$('#editor-paint .current')?.classList.remove('current');$('#editor-paint').children[row-1]?.classList.add('current');}
$('#code-editor').onscroll=syncEditorScroll;
for(const event of ['click','keyup','select','focus'])$('#code-editor').addEventListener(event,updateEditorPosition);
applyEditorFont();


let functionSearchVersion=0;
let semanticEnabled=false;
function closeSearchResults(){
  $('#code-search-dropdown').hidden=true;++functionSearchVersion;
}
function resetSearchResults(){
  closeSearchResults();$('#function-search-results').replaceChildren();$('#function-search-notice').textContent='';$('#function-context').hidden=true;
}
$('#function-query').addEventListener('input',()=>{if(!$('#function-query').value.trim())resetSearchResults();});
$('#function-query').addEventListener('focus',()=>{if($('#function-query').value.trim()&&$('#function-search-results').childNodes.length)$('#code-search-dropdown').hidden=false;});
$('#function-search-mode').addEventListener('change',()=>{if($('#function-query').value.trim())$('#function-search-form').requestSubmit();});
document.addEventListener('keydown',e=>{
  if(document.body.dataset.page!=='code'||document.querySelector('dialog[open]'))return;
  if((e.ctrlKey||e.metaKey)&&e.shiftKey&&e.key.toLowerCase()==='f'){e.preventDefault();$('#function-query').focus();$('#function-query').select();}
  if(e.key==='Escape'){closeSearchResults();$('#code-search-settings').open=false;}
});
document.addEventListener('click',e=>{if(!$('#function-search-panel').contains(e.target)){closeSearchResults();$('#code-search-settings').open=false;}});
$('#code-search-settings').addEventListener('toggle',()=>{if($('#code-search-settings').open){closeSearchResults();updateKnowledgeStatus();}});
async function updateKnowledgeStatus(){
  const version=functionSearchVersion;
  try{const state=await api('knowledge');if(version!==functionSearchVersion)return;const status=$('#knowledge-status');const sem=state.semantic;semanticEnabled=!!sem?.enabled;const button=$('#semantic-toggle');button.disabled=!state.ready;button.textContent=semanticEnabled?'Приостановить индексацию / поиск':sem?.indexed?'Продолжить смысловую индексацию':'Включить смысловой поиск';let semanticText=semanticEnabled?(sem.ready?`${sem.model} · индекс готов (${sem.indexed}/${sem.total})`:sem.job.status==='error'?`Ошибка модели: ${sem.job.error}`:`${sem.job.phase||'Индексация'} · ${sem.job.done||0}/${sem.job.total||sem.total}`):`Смысловая индексация приостановлена · сохранено ${sem?.indexed||0}/${sem?.total||0} фрагментов. Поиск по объектам и коду доступен.`;status.textContent=state.ready?`SQLite · BSL / ANTLR · ${state.modules} модулей · ${state.symbols} методов · ${state.warnings} замечаний разбора. ${semanticText}`:'Откройте проект и дождитесь индексации.';
    if(state.diagnostics?.length){const details=document.createElement('details'),summary=document.createElement('summary');summary.textContent='Замечания синтаксического разбора';details.append(summary);for(const warning of state.diagnostics){const p=document.createElement('p');p.textContent=`${warning.path}:${warning.line}:${warning.column||1} — ${warning.message}`;details.append(p);}status.append(details);}
  }catch(e){if(version===functionSearchVersion)$('#knowledge-status').textContent=e.message;}
}
$('#semantic-toggle').onclick=async()=>{const button=$('#semantic-toggle');button.disabled=true;try{await api('embeddings',{enabled:!semanticEnabled});await updateKnowledgeStatus();}catch(e){toast(e.message);}finally{button.disabled=false;}};
setInterval(()=>{if(!$('#function-search-panel').hidden&&$('#code-search-settings').open)updateKnowledgeStatus();},3000);
$('#close-function-search').onclick=closeSearchResults;
$('#function-search-form').onsubmit=async e=>{
  e.preventDefault();if(!$('#function-query').value.trim()){resetSearchResults();return;}$('#code-search-settings').open=false;$('#code-search-dropdown').hidden=false;const request=++functionSearchVersion;const panel=$('#function-search-results');panel.textContent='Поиск…';$('#function-context').hidden=true;
  try{const response=await api('search-functions?q='+encodeURIComponent($('#function-query').value)+'&mode='+$('#function-search-mode').value);if(request!==functionSearchVersion)return;panel.replaceChildren();$('#function-search-notice').textContent=response.warning||({hybrid:'Результаты по коду и смыслу',semantic:'Результаты по смыслу',lexical:'Результаты по объектам и ключевым словам'}[response.mode]);
    if(!response.results.length){panel.textContent='Ничего не найдено. Попробуйте имя функции или слова из её комментария.';return;}
    for(const item of response.results){const row=document.createElement('article');row.className='function-result';const open=document.createElement('button');open.className='function-result-name';open.textContent=`${item.name}(${item.parameters.replace(/\s+/g,' ')})${item.export?' · Экспорт':''}`;open.onclick=()=>openFunctionResult(item);const location=document.createElement('small');location.textContent=`${item.owner_name} · ${item.module_name} · строки ${item.declaration_line}–${item.end_line}${item.match?' · '+({hybrid:'Код + смысл',semantic:'По смыслу',lexical:'По коду','object-code':'Код выбранного объекта',keywords:'Ключевые слова'}[item.match]):''}`;const comment=document.createElement('p');comment.textContent=item.comment||item.path;const context=document.createElement('button');context.textContent='Код и вызовы';context.onclick=()=>showFunctionContext(item.id);row.append(open,location,comment,context);panel.append(row);}
  }catch(e){if(request===functionSearchVersion)panel.textContent=e.message;}
};
async function openFunctionResult(item){
  document.dispatchEvent(new CustomEvent('athena-open-code'));
  if(!nodes.has(item.owner_id)){toast('Проект изменился. Повторите поиск.');return;}
  selectNode(item.owner_id,'code');$('#files').value=item.file_id;await loadFile();
  if(!file||currentFileId!==item.file_id||file.hash!==item.file_hash){toast('Исходник изменился после индексации. Обновите проект и повторите поиск.');return;}
  if(hasDraft()){toast('Открыт локальный черновик. Строки результата относятся к сохранённой версии.');return;}
  closeSearchResults();$('#ai-panel').hidden=true;
  const input=$('#code-editor'),lines=input.value.split('\n');const offset=lines.slice(0,item.declaration_line-1).reduce((n,line)=>n+line.length+1,0);
  input.focus();input.setSelectionRange(offset,offset);input.scrollTop=Math.max(0,(item.declaration_line-3)*Number(getComputedStyle(input).lineHeight.replace('px','')));syncEditorScroll();updateEditorPosition();
}
async function showFunctionContext(id){
  const version=++functionSearchVersion;const panel=$('#function-context');panel.hidden=false;panel.textContent='Чтение функции…';
  try{const result=await api('function?id='+encodeURIComponent(id));if(version!==functionSearchVersion)return;panel.replaceChildren();const title=document.createElement('h3');title.textContent=result.name;const code=document.createElement('pre');code.textContent=result.code;const heading=document.createElement('h4');heading.textContent='Вызовы внутри метода';panel.append(title);if(!result.syntax_valid){const warning=document.createElement('p');warning.className='warning';warning.textContent='В методе есть ошибки синтаксиса. Связи вызовов не подтверждены.';panel.append(warning);}panel.append(code,heading);
    for(const call of result.outgoing){const p=document.createElement('p');p.textContent=`${call.receiver?call.receiver+'.':''}${call.name} · строка ${call.line} · ${call.resolution==='local'?'метод того же модуля':call.resolution==='candidate'?'возможная ссылка на общий модуль':'цель не определена'}`;panel.append(p);}
    if(!result.outgoing.length){const p=document.createElement('p');p.textContent='Вызовы не обнаружены.';panel.append(p);}
    const incoming=document.createElement('h4');incoming.textContent='Кто вызывает';panel.append(incoming);
    for(const call of result.incoming){const b=document.createElement('button');b.textContent=`${call.name} · строка ${call.line}${call.resolution==='candidate'?' · предположение':''}`;b.onclick=()=>showFunctionContext(call.source_symbol);panel.append(b);}
    if(!result.incoming.length){const p=document.createElement('p');p.textContent='В индексе нет установленных или предполагаемых входящих вызовов.';panel.append(p);}
  }catch(e){if(version===functionSearchVersion)panel.textContent=e.message;}
}

initialize();
setInterval(pollAuto, 2000);
let aiSettings=null,aiJob=null,aiViewVersion=0,aiTimer=null,aiEngine='ai';
const aiJobPath=()=>aiEngine==='agent'?'agent/':'ai/';
function questionAgentView(job){
  return {...job,kind:'agent',sources:[],warnings:[],
    status:job.transport_pending&&job.status==='running'?'running':job.status==='stopped'?'error':job.status,
    error:job.error||job.phase,answer:job.answer||'',steps:job.steps||[]};
}
$('#rail-ai').onclick=()=>{$('main').scrollTop=0;$('#ai-panel').hidden=false;$('#function-search-panel').hidden=true;$('#changes-panel').hidden=true;$('#ai-question').focus({preventScroll:true});};
$('#ai-close').onclick=()=>{$('#ai-panel').hidden=true;};
const aiCapabilities=document.createElement('dialog');aiCapabilities.id='ai-capabilities';
aiCapabilities.innerHTML='<div class="dialog-title"><h2>Функции помощника</h2><button type="button" aria-label="Закрыть список функций">×</button></div><p class="muted">Вопрос по коду: инструменты чтения. Агент кода: исследование или предложения в отдельном наборе. Создание набора выполняется автоматически; загрузка в 1С остаётся действием пользователя.</p><p id="ai-capabilities-status" role="status"></p><div id="ai-capabilities-list"></div>';
document.body.append(aiCapabilities);aiCapabilities.querySelector('button').onclick=()=>aiCapabilities.close();
$('#ai-capabilities-open').onclick=async()=>{
  aiCapabilities.showModal();$('#ai-capabilities-status').textContent='Загружаем доступные функции…';$('#ai-capabilities-list').replaceChildren();
  try{
    const catalog=await api('agent/tools');
    $('#ai-capabilities-status').textContent=`${catalog.tools.length} функций · ${(catalog.modes?.analyze||[]).length} доступны для вопросов и исследования`;
    for(const tool of catalog.tools){
      const row=document.createElement('article'),name=document.createElement('strong'),description=document.createElement('p'),mode=document.createElement('small');
      name.textContent=tool.function.name;description.textContent=tool.function.description;
      mode.textContent=catalog.automatic?.includes(tool.function.name)?'Автоматически при запуске предложений':catalog.modes?.analyze.includes(tool.function.name)?'Вопросы и исследование · предложения':'Агент кода · предложения';
      row.append(name,mode,description);$('#ai-capabilities-list').append(row);
    }
  }catch(e){$('#ai-capabilities-status').textContent=e.message;}
};
$('#ai-settings-close').onclick=()=>$('#ai-settings-dialog').close();
function aiRemoteVisibility(){let local=false;try{const host=new URL($('#ai-url').value).hostname;local=host==='localhost'||host==='[::1]'||/^127\./.test(host);}catch{}$('#ai-remote-label').hidden=local;}
function fillAISettings(){const p=aiSettings.profiles[$('#ai-provider').value];$('#ai-url').value=p.base_url;$('#ai-model').value=p.model;$('#ai-timeout').value=p.timeout;$('#ai-budget').value=p.context_chars;$('#ai-remote').checked=p.allow_remote_code;$('#ai-key').value='';$('#ai-key').placeholder=p.has_api_key?'Ключ уже в памяти; оставьте пустым, чтобы сохранить':'Хранится только до закрытия Athena';$('#ai-clear-key').checked=false;$('#ai-model-list').replaceChildren();$('#ai-settings-status').textContent='Настройки обоих подключений сохраняются отдельно. API-ключ не записывается на диск.';aiRemoteVisibility();}
$('#ai-provider').onchange=fillAISettings;
$('#ai-url').oninput=()=>{$('#ai-remote').checked=false;aiRemoteVisibility();};
$('#ai-settings-open').onclick=async()=>{try{aiSettings=await api('ai/settings');$('#ai-provider').value=aiSettings.provider;fillAISettings();$('#ai-settings-dialog').showModal();}catch(e){toast(e.message);}};
async function saveAISettings(){const data={provider:$('#ai-provider').value,base_url:$('#ai-url').value,model:$('#ai-model').value,timeout:Number($('#ai-timeout').value),context_chars:Number($('#ai-budget').value),allow_remote_code:$('#ai-remote').checked,api_key:$('#ai-key').value,clear_key:$('#ai-clear-key').checked};aiSettings=await api('ai/settings',data);$('#ai-key').value='';$('#ai-clear-key').checked=false;return aiSettings;}
$('#ai-settings-form').onsubmit=async e=>{e.preventDefault();try{await saveAISettings();$('#ai-settings-dialog').close();toast('Подключение AI сохранено');}catch(e){$('#ai-settings-status').textContent=e.message;}};
$('#ai-test').onclick=async()=>{const button=$('#ai-test');button.disabled=true;$('#ai-settings-status').textContent='Проверка соединения…';try{if(!$('#ai-settings-form').reportValidity())return;await saveAISettings();const result=await api('ai/models',{});$('#ai-model-list').replaceChildren();for(const name of result.models){const option=document.createElement('option');option.value=name;$('#ai-model-list').append(option);}if(!$('#ai-model').value&&result.models.length===1)$('#ai-model').value=result.models[0];$('#ai-settings-status').textContent=result.message+` Доступно моделей: ${result.models.length}. Выберите модель и сохраните настройки.`;}catch(e){$('#ai-settings-status').textContent=e.message;}finally{button.disabled=false;}};
function setAIBusy(busy){$('#ai-answer-button').disabled=$('#ai-context-button').disabled=busy;$('#ai-cancel-button').hidden=!busy;$('#ai-question-form').setAttribute('aria-busy',String(busy));}
function resetAIView(){++aiViewVersion;clearTimeout(aiTimer);if(aiJob)api(aiJobPath()+'cancel',{id:aiJob}).catch(()=>{});aiJob=null;setAIBusy(false);$('#ai-answer').replaceChildren();$('#ai-sources').replaceChildren();$('#ai-warnings').textContent='';$('#ai-status').textContent='';$('#ai-response').hidden=true;aiAnswerText='';}
// Render only supported Markdown into DOM nodes. Model text is never HTML.
function renderAIMarkdown(container,markdown,sources=new Map()){
  const inline=(parent,text,depth=0)=>{
    if(depth>12){parent.append(document.createTextNode(text));return;}
    const pattern=/\\[\\`*_\[\]~]|(`+)([^\n]*?)\1|\[S\d+\]|\*\*([^\n]+?)\*\*|__([^\n]+?)__|~~([^\n]+?)~~|(?<!\*)\*([^*\n]+?)\*(?!\*)|(?<!\w)_([^_\n]+?)_(?!\w)|\[([^\]\n]+)\]\(([^\s)]+)\)/g;
    let offset=0;
    for(const match of text.matchAll(pattern)){
      parent.append(document.createTextNode(text.slice(offset,match.index)));
      const token=match[0];let element;
      if(token.startsWith('\\'))parent.append(document.createTextNode(token.slice(1)));
      else if(match[1]){element=document.createElement('code');element.textContent=match[2];parent.append(element);}
      else if(/^\[S\d+\]$/.test(token)){
        const source=sources.get(token.slice(1,-1));
        parent.append(source?aiSourceButton(source,token.slice(1,-1)):document.createTextNode(token));
      }else if(match[3]||match[4]||match[5]||match[6]||match[7]){
        element=document.createElement(match[3]||match[4]?'strong':match[5]?'del':'em');
        inline(element,match[3]||match[4]||match[5]||match[6]||match[7],depth+1);parent.append(element);
      }else{
        // External links remain deliberate user actions; embedded images are not loaded.
        let url;try{url=new URL(match[9]);}catch{}
        if(url&&['http:','https:'].includes(url.protocol)){
          element=document.createElement('a');element.href=url.href;element.target='_blank';element.rel='noopener noreferrer';element.textContent=match[8];parent.append(element);
        }else parent.append(document.createTextNode(match[8]));
      }
      offset=match.index+token.length;
    }
    parent.append(document.createTextNode(text.slice(offset)));
  };
  const item=line=>line.match(/^(\s*)([-+*]|\d+[.)])\s+(.+)$/);
  const indent=match=>match[1].replace(/\t/g,'    ').length;
  const fence=line=>line.match(/^\s*(`{3,}|~{3,})([\w+-]*)\s*$/);
  const divider=line=>/^\s*(?:-{3,}|\*{3,}|_{3,})\s*$/.test(line);
  const tableDivider=line=>/^\s*\|?\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)+\|?\s*$/.test(line);
  const cells=line=>line.trim().replace(/^\||\|$/g,'').split(/(?<!\\)\|/).map(v=>v.trim().replace(/\\\|/g,'|'));
  const blocks=(parent,lines,depth=0)=>{
    if(depth>12){parent.append(document.createTextNode(lines.join('\n')));return;}
    let i=0;
    const list=start=>{
      const first=item(lines[start]),base=indent(first),ordered=/\d/.test(first[2]);
      const el=document.createElement(ordered?'ol':'ul');let pos=start;
      if(ordered)el.start=Number(first[2].replace(/\D/g,''));
      while(pos<lines.length){
        const current=item(lines[pos]);
        if(!current||indent(current)!==base||/\d/.test(current[2])!==ordered)break;
        const li=document.createElement('li');inline(li,current[3]);pos++;
        while(pos<lines.length){
          const nested=item(lines[pos]);
          if(nested&&indent(nested)>base){const next=list(pos);li.append(next.element);pos=next.end;}
          else if(!nested&&lines[pos].trim()&&lines[pos].match(/^\s*/)[0].length>base){li.append(document.createTextNode(' '));inline(li,lines[pos].trim());pos++;}
          else if(!lines[pos].trim()&&item(lines[pos+1]||'')&&indent(item(lines[pos+1]))>=base)pos++;
          else break;
        }
        el.append(li);
      }
      return {element:el,end:pos};
    };
    while(i<lines.length){
      const line=lines[i];if(!line.trim()){i++;continue;}
      const opening=fence(line),heading=line.match(/^\s{0,3}(#{1,6})\s+(.+?)(?:\s+#+)?$/);
      if(opening){
        const codeLines=[];i++;
        while(i<lines.length){const closing=fence(lines[i]);if(closing&&closing[1][0]===opening[1][0]&&closing[1].length>=opening[1].length){i++;break;}codeLines.push(lines[i++]);}
        const block=document.createElement('div');block.className='ai-code-block';
        const bar=document.createElement('div');bar.className='ai-code-heading';
        const language=document.createElement('span');language.textContent=opening[2]||'Код';
        const copy=document.createElement('button');copy.type='button';copy.textContent='Копировать';copy.setAttribute('aria-label','Копировать блок кода');
        copy.onclick=async()=>{try{await navigator.clipboard.writeText(codeLines.join('\n'));copy.textContent='Скопировано';}catch{toast('Не удалось скопировать код');}};
        const pre=document.createElement('pre'),code=document.createElement('code');code.textContent=codeLines.join('\n');pre.append(code);bar.append(language,copy);block.append(bar,pre);parent.append(block);continue;
      }
      if(heading){const h=document.createElement('h'+heading[1].length);inline(h,heading[2]);parent.append(h);i++;continue;}
      if(divider(line)){parent.append(document.createElement('hr'));i++;continue;}
      if(/^\s*>/.test(line)){
        const quote=[];while(i<lines.length&&/^\s*>/.test(lines[i]))quote.push(lines[i++].replace(/^\s*>\s?/,''));
        const el=document.createElement('blockquote');blocks(el,quote,depth+1);parent.append(el);continue;
      }
      if(item(line)){const result=list(i);parent.append(result.element);i=result.end;continue;}
      if(line.includes('|')&&i+1<lines.length&&tableDivider(lines[i+1])){
        const wrapper=document.createElement('div');wrapper.className='ai-table-scroll';
        const table=document.createElement('table'),head=document.createElement('thead'),body=document.createElement('tbody');
        const row=(values,header)=>{const tr=document.createElement('tr');for(const value of values){const cell=document.createElement(header?'th':'td');if(header)cell.scope='col';inline(cell,value);tr.append(cell);}return tr;};
        head.append(row(cells(line),true));i+=2;
        while(i<lines.length&&lines[i].trim()&&lines[i].includes('|'))body.append(row(cells(lines[i++]),false));
        table.append(head,body);wrapper.append(table);parent.append(wrapper);continue;
      }
      const paragraph=[line.trim()];i++;
      while(i<lines.length&&lines[i].trim()&&!fence(lines[i])&&!/^\s{0,3}#{1,6}\s/.test(lines[i])&&!/^\s*>/.test(lines[i])&&!item(lines[i])&&!divider(lines[i])&&!(lines[i].includes('|')&&i+1<lines.length&&tableDivider(lines[i+1])))paragraph.push(lines[i++].trim());
      const p=document.createElement('p');inline(p,paragraph.join(' '));parent.append(p);
    }
  };
  const content=document.createDocumentFragment();blocks(content,String(markdown||'').replace(/\r\n?/g,'\n').split('\n'));container.replaceChildren(content);
}
let aiAnswerText='';
$('#ai-copy-answer').onclick=async()=>{try{await navigator.clipboard.writeText(aiAnswerText);toast('Ответ скопирован');}catch{toast('Не удалось скопировать ответ');}};
$('.ai-tools').addEventListener('click',event=>{if(event.target.closest('button,a'))$('.ai-tools').open=false;});
function aiSourceButton(source,label){
  const button=document.createElement('button');button.type='button';button.className='ai-citation';button.textContent=label;button.title=source.name+' · '+source.path;
  button.onclick=async()=>{
    if(source.type==='reference'){
      const dialog=document.createElement('dialog'),pre=document.createElement('pre'),close=document.createElement('button');
      dialog.className='knowledge-read-dialog';pre.textContent=source.context;close.textContent='Закрыть';close.onclick=()=>dialog.close();
      dialog.append(pre,close);dialog.onclose=()=>dialog.remove();document.body.append(dialog);dialog.showModal();return;
    }
    document.dispatchEvent(new CustomEvent('athena-open-code'));
    if(source.type!=='metadata')return openFunctionResult({...source,declaration_line:source.start_line});
    try{
      const state=await api('knowledge');
      if(state.snapshot!==source.snapshot||!nodes.has(source.object_id)){toast('Проект изменился. Повторите вопрос.');return;}
      selectNode(source.object_id,'overview');$('#ai-panel').hidden=true;
    }catch(e){toast(e.message);}
  };
  return button;
}
function renderAIJob(job){
  $('#ai-response').hidden=false;$('#ai-response').dataset.state=job.status;
  $('#ai-status').textContent=job.status==='error'?job.error:job.status==='cancelled'?'Запрос отменён. Сервер может завершить обработку позднее.':job.status==='running'?job.phase:job.kind==='metadata'?`Ответ по метаданным · ${job.inventory_total} объектов · ${job.inventory_complete?'полный список':'показана часть списка'}`:job.kind==='agent'?`Ответ · ${job.model} · ${job.tool_calls} вызовов инструментов`:job.kind==='answer'?`Ответ · ${job.model} · ${job.sources.length} источников`:'Контекст проекта · без генерации ответа';
  $('#ai-steps-panel').hidden=!(job.steps||[]).length;
  $('#ai-steps').replaceChildren();
  for(const step of job.steps||[]){const li=document.createElement('li');li.textContent=`${step.ok?'✓':'⚠'} ${step.tool} · ${step.detail}`;$('#ai-steps').append(li);}
  $('#ai-warnings').textContent=(job.warnings||[]).join('\n');
  const answer=$('#ai-answer');aiAnswerText=job.answer||'';$('#ai-copy-answer').hidden=!aiAnswerText;
  const sourceMap=new Map((job.sources||[]).map(s=>[s.label,s]));
  renderAIMarkdown(answer,aiAnswerText,sourceMap);
  $('#ai-sources-panel').hidden=!(job.sources||[]).length;$('#ai-source-count').textContent=(job.sources||[]).length||'';
  const sources=$('#ai-sources');sources.replaceChildren();
  for(const source of job.sources||[]){
    const metadata=source.type==='metadata';
    const card=document.createElement('article');card.className='function-result';
    const name=aiSourceButton(source,`${source.name}`);name.className='ai-source-name';
    const badge=document.createElement('span');badge.className='ai-source-badge';badge.textContent=source.label;
    const heading=document.createElement('div');heading.className='ai-source-heading';heading.append(badge,name);card.append(heading);
    const location=document.createElement('small');
    location.textContent=source.path+(metadata?' · метаданные':` · строки ${source.start_line}–${source.end_line}`)+(source.truncated?' · фрагмент':'');
    const details=document.createElement('details'),summary=document.createElement('summary'),code=document.createElement('pre');
    summary.textContent='Посмотреть контекст';code.textContent=source.code;
    details.append(summary,code);card.append(location,details);sources.append(card);
  }
  document.dispatchEvent(new CustomEvent('athena-ai-rendered',{detail:job}));
}
async function askAI(contextOnly){if(!$('#ai-question-form').reportValidity())return;const version=++aiViewVersion;clearTimeout(aiTimer);setAIBusy(true);$('#ai-response').hidden=false;$('#ai-response').dataset.state='running';$('#ai-copy-answer').hidden=true;$('#ai-sources-panel').hidden=true;$('#ai-steps-panel').hidden=true;$('#ai-status').textContent='Подготовка запроса…';$('#ai-answer').replaceChildren();$('#ai-sources').replaceChildren();$('#ai-warnings').textContent='';try{const started=await api(contextOnly?'ai/ask':'assistant/ask',{question:$('#ai-question').value,context_only:contextOnly});const engine=started.engine||'ai',path=engine==='agent'?'agent/':'ai/';if(version!==aiViewVersion){api(path+'cancel',{id:started.id}).catch(()=>{});return;}aiEngine=engine;aiJob=started.id;async function poll(){try{const job=await api(path+'job?id='+encodeURIComponent(started.id));if(version!==aiViewVersion)return;renderAIJob(engine==='agent'?questionAgentView(job):job);if(job.status==='running'||job.transport_pending)aiTimer=setTimeout(poll,1000);else{aiJob=null;setAIBusy(false);}}catch(e){if(version===aiViewVersion){$('#ai-status').textContent=e.message;setAIBusy(false);}}}await poll();}catch(e){if(version===aiViewVersion){$('#ai-status').textContent=e.message;setAIBusy(false);}}}
$('#ai-question-form').onsubmit=e=>{e.preventDefault();askAI(false);};
$('#ai-context-button').onclick=()=>askAI(true);
$('#ai-cancel-button').onclick=async()=>{if(!aiJob)return;try{const job=await api(aiJobPath()+'cancel',{id:aiJob});renderAIJob(aiEngine==='agent'?questionAgentView(job):job);++aiViewVersion;clearTimeout(aiTimer);aiJob=null;setAIBusy(false);}catch(e){toast(e.message);}};

function updateIndexProgress(job){
  const panel=$('#index-progress');panel.hidden=job.status!=='running';if(panel.hidden)return;
  let [phase,...path]=(job.phase||'Подготовка').split(' · ');
  const saving=phase==='Сохранение индекса SQLite'||(phase==='Связывание вызовов SQLite'&&job.total>0&&job.done===job.total);
  if(saving)phase='Сохранение индекса SQLite';
  $('#index-progress-phase').textContent=phase;
  $('#index-progress-file').textContent=path.join(' · ');
  const bar=$('#index-progress-bar');if(job.total&&!saving){bar.max=job.total;bar.value=job.done||0;}else bar.removeAttribute('value');
  $('#index-progress-count').textContent=saving?'Все методы обработаны · запись индекса':job.total?`${job.done||0} / ${job.total}`:'Обработка…';
  $('#index-progress-time').textContent=job.elapsed!==undefined?`${Math.floor(job.elapsed/60)} мин ${job.elapsed%60} с`:'';
}

const indexSettingsForm=$('#index-settings-form');
function describeIndexMode(){ $('#index-mode-description').textContent=indexSettingsForm.elements.mode.value==='comfort'?'Меньше нагрузки и паузы между пакетами. Можно продолжать работу в IDE.':'Больше ресурсов для скорейшего построения индекса. Другие приложения могут работать медленнее.'; }
$('#index-settings-open').onclick=async()=>{try{const state=await api('embedding-settings');const config=state.settings;for(const [key,value] of Object.entries(config))indexSettingsForm.elements[key].value=value;describeIndexMode();$('#index-settings-notice').textContent='';$('#index-settings').showModal();}catch(e){toast(e.message);}};
$('#index-settings-close').onclick=()=>$('#index-settings').close();
indexSettingsForm.elements.mode.onchange=()=>{const defaults=indexSettingsForm.elements.mode.value==='comfort'?{threads:2,batch_size:8,token_budget:2048,gpu_memory_mb:2048,pause_ms:100}:{threads:4,batch_size:32,token_budget:8192,gpu_memory_mb:4096,pause_ms:0};for(const [key,value] of Object.entries(defaults))indexSettingsForm.elements[key].value=value;describeIndexMode();};
indexSettingsForm.onsubmit=async(event)=>{event.preventDefault();const config={};for(const key of ['device','mode','threads','batch_size','token_budget','gpu_memory_mb','pause_ms'])config[key]=['device','mode'].includes(key)?indexSettingsForm.elements[key].value:Number(indexSettingsForm.elements[key].value);try{const state=await api('embedding-settings');if(state.running)throw new Error('Приостановите индексацию и дождитесь остановки, затем сохраните настройки.');await api('embeddings',{enabled:false,settings:config});$('#index-settings').close();await updateKnowledgeStatus();toast('Настройки индексации сохранены');}catch(e){$('#index-settings-notice').textContent=e.message;}};

$("#ai-index-settings-open").onclick=()=>$("#index-settings-open").onclick();

// Main workspace follows the home / code / documentation layout.
(()=>{
  const workspace=$('.workspace'),main=$('main');
  let page='home',history=[],latestSources=[],refreshing=false;
  const historyKey='athena-question-history-v1';
  try{const saved=JSON.parse(localStorage.getItem(historyKey)||'[]');if(Array.isArray(saved))history=saved.filter(x=>x&&typeof x.question==='string'&&typeof x.id==='string'&&typeof x.project==='string'&&Number.isFinite(x.time)&&['ai','agent'].includes(x.engine)).slice(0,30);}catch{}
  const projectKey=()=>connectionData.active||project?.root||'';
  const titles={home:'Главная',code:'Код',docs:'Документация',history:'История',assistant:'AI-помощник'};
  const navIds={home:'rail-ai',code:'rail-objects',docs:'nav-docs',history:'nav-history',assistant:'nav-assistant'};
  const scope=document.createElement('select');scope.id='question-scope';scope.setAttribute('aria-label','Область вопроса');
  const scopeOption=document.createElement('option');scopeOption.textContent='Текущая конфигурация';scopeOption.value='current';scope.append(scopeOption);
  $('.ai-actions').prepend(scope);$('.ai-composer-hint').hidden=true;
  $('#ai-panel .ai-header h2').textContent='Вопрос по конфигурации';
  $('#ai-panel .ai-eyebrow').textContent='ATHENA ASSISTANT';
  $('#ai-panel .ai-body>p').textContent='Задайте вопрос — Athena найдёт ответ в метаданных и коде вашего проекта.';
  $('#ai-question').placeholder='Что хотите узнать о конфигурации?\nНапример: где рассчитывается сумма заказа?\nКакие методы вызываются при записи документа?\nКакие правила проекта нужно соблюдать?';$('#ai-answer-button').textContent='Спросить';
  const shortcuts=document.createElement('div');shortcuts.className='home-shortcuts';
  shortcuts.innerHTML='<button type="button" data-prompt="Объясни назначение основных методов конфигурации и укажи, в каких модулях они находятся.">Объяснить метод <span>↗</span></button><button type="button" data-prompt="Объясни связи между документами и справочниками конфигурации. Приведи подтверждения из метаданных и кода.">Связи <span>↗</span></button>';
  for(const [label,prompt] of [
    ['Расчёты','Найди, где в конфигурации рассчитываются суммы документов. Укажи методы, формулы и строки кода.'],
    ['Цепочка вызовов','Покажи цепочку вызовов при записи документа: обработчики, вызываемые методы и задействованные модули. Если документов несколько, помоги выбрать нужный.'],
    ['Работа с данными','Найди, где документ читает и записывает данные регистров. Укажи объекты и подтверждения из кода.'],
    ['Проверить код','Помоги выбрать метод для проверки и найди в нём возможные ошибки. Объясни причины и отдели подтверждённые проблемы от предположений. Код не изменяй.'],
    ['Правила проекта','Какие обязательные правила и проверенные исправления есть для текущего проекта? Приведи источники и объясни, как применять их при разработке.'],
    ['Состав конфигурации','Какие документы есть в конфигурации? Покажи полный список с именами и синонимами.']
  ]){const button=document.createElement('button');button.type='button';button.dataset.prompt=prompt;button.textContent=label+' ↗';button.title=prompt;shortcuts.append(button);}
  const contextButton=$('#ai-context-button');contextButton.textContent='Найти данные';contextButton.title='Найти контекст для введённого вопроса без генерации ответа';shortcuts.insertBefore(contextButton,shortcuts.lastElementChild);
  $('#ai-question-form').after(shortcuts);
  shortcuts.addEventListener('click',e=>{const b=e.target.closest('[data-prompt]');if(b){$('#ai-question').value=b.dataset.prompt;$('#ai-question').focus();}});
  const sections=document.createElement('div');sections.className='home-dashboard-sections';
  sections.innerHTML='<section class="dashboard-section"><div class="dashboard-section-heading"><h2>Последние вопросы</h2><button type="button" id="home-all-history">Вся история <span>↗</span></button></div><div id="home-recent-questions" class="question-list"></div></section><section class="dashboard-section"><div class="dashboard-section-heading"><h2>Документация</h2><span class="section-caption">СПРАВКА И МЕТАДАННЫЕ</span></div><a class="dashboard-document" href="http://127.0.0.1:8767/" target="_blank" rel="noopener"><span class="document-icon" aria-hidden="true">▤</span><span><strong>Справка платформы 1С</strong><small>Типы, методы и свойства платформы</small></span><span class="document-open">Открыть ↗</span></a></section>';
  $('.ai-body').append(sections);
  const views=document.createElement('div');views.id='dashboard-views';views.hidden=true;
  views.innerHTML='<section id="dashboard-docs" hidden><span class="page-eyebrow">БАЗА ЗНАНИЙ</span><h1>Документация</h1><p class="page-description">Справка платформы и структура сохранённой конфигурации.</p><a class="dashboard-document" href="http://127.0.0.1:8767/" target="_blank" rel="noopener"><span class="document-icon">▤</span><span><strong>Синтаксис-помощник 1С</strong><small>Справка по импортированной версии платформы</small></span><span class="document-open">Открыть ↗</span></a><button type="button" id="docs-open-configuration" class="dashboard-document"><span class="document-icon">⌘</span><span><strong>Структура конфигурации</strong><small>Объекты, реквизиты, табличные части и модули</small></span><span class="document-open">Открыть ↗</span></button></section><section id="dashboard-history" hidden><span class="page-eyebrow">РАБОТА С ПРОЕКТОМ</span><h1>История</h1><p class="page-description">Вопросы по текущей конфигурации. Выберите запись, чтобы открыть сохранённый ответ.</p><div id="dashboard-history-list" class="question-list"></div></section>';
  const imports=document.createElement('section');imports.className='documentation-imports';
  imports.innerHTML='<h2>Материалы проекта</h2><div class="documentation-import-grid"><article class="documentation-import-card"><div class="import-card-heading"><span class="import-card-icon" aria-hidden="true">⌘</span><span class="coming-soon-badge">Скоро</span></div><h3>Git-проект</h3><p>Подключение репозитория с исходным кодом проекта.</p><label for="documentation-git-url">Адрес репозитория</label><input id="documentation-git-url" type="url" disabled placeholder="https://github.com/organization/project"><button type="button" disabled>Загрузить Git-проект</button><small>Подключение Git пока недоступно.</small></article><article class="documentation-import-card"><div class="import-card-heading"><span class="import-card-icon" aria-hidden="true">▤</span><span class="coming-soon-badge">Скоро</span></div><h3>Документация проекта</h3><p>Загрузка описаний, инструкций и других документов проекта.</p><div class="documentation-drop-placeholder"><span aria-hidden="true">↑</span><strong>Файлы документации</strong><small>Здесь можно будет выбрать файлы для загрузки</small></div><button type="button" disabled>Загрузить документацию</button><small>Загрузка файлов пока недоступна.</small></article></div>';
  views.querySelector('#dashboard-docs .page-description').after(imports);
  main.append(views);
  const panel=document.createElement('aside');panel.id='project-panel';panel.setAttribute('aria-label','Сведения о проекте');
  panel.innerHTML='<div class="project-panel-heading"><h2>Проект</h2><span class="project-chip">1С</span></div><div class="project-name"><span class="project-avatar">A</span><div><strong id="dashboard-project-name">Конфигурация не открыта</strong><small id="dashboard-project-subtitle">Выберите файловую выгрузку</small></div></div><div class="project-status-block"><div class="status-title">Статус индекса <i id="dashboard-index-dot" class="status-dot"></i></div><strong id="dashboard-index-status">Ожидает проекта</strong><div class="status-track"><span id="dashboard-index-progress"></span></div><small id="dashboard-index-details">Метаданные и код</small></div><div class="project-status-block"><div class="status-title">Статус модели <i id="dashboard-model-dot" class="status-dot"></i></div><strong id="dashboard-model-status">Проверяем настройки…</strong><small id="dashboard-model-details"></small><button type="button" id="dashboard-model-settings" class="text-action">Настроить подключение ↗</button></div><div class="project-status-block"><div class="status-title">Локальная обработка <i class="status-dot ready"></i></div><strong>Индекс на компьютере</strong><small id="dashboard-local-details">Разбор метаданных и BSL выполняется локально.</small></div><section class="project-source-section"><h3>Ссылки на исходный код</h3><div id="dashboard-source-links"></div><button type="button" id="dashboard-browse-code" class="text-action">Открыть конфигурацию ↗</button></section><div class="project-panel-footnote"><span>⌘</span> Ответы можно проверить по исходникам проекта.</div>';
  workspace.append(panel);
  function setPage(value){
    page=value;document.body.dataset.page=value;
    $('#page-title').textContent=titles[value];
    for(const [name,id] of Object.entries(navIds)){const b=$('#'+id);b.classList.toggle('active',name===value);if(name===value)b.setAttribute('aria-current','page');else b.removeAttribute('aria-current');}
    $('#rail-changes').classList.remove('active');$('#nav-agent').classList.remove('active');$('#nav-agent').removeAttribute('aria-current');
    $('#ai-panel').hidden=!['home','assistant'].includes(value);$('#dashboard-views').hidden=!['docs','history'].includes(value);
    for(const name of ['docs','history'])$('#dashboard-'+name).hidden=value!==name;
    $('#function-search-panel').hidden=value!=='code';
    if(value!=='code'){$('#changes-panel').hidden=true;closeSearchResults();$('#code-search-settings').open=false;}else updateKnowledgeStatus();
    if(['home','assistant'].includes(value)){$('.ai-mode-tabs button')?.click();}
    main.scrollTop=0;
  }
  $('#rail-ai').addEventListener('click',()=>{setPage('home');refresh();});
  $('#rail-objects').addEventListener('click',()=>setPage('code'));
  $('#nav-assistant').onclick=()=>{setPage('assistant');$('#ai-question').focus({preventScroll:true});refresh();};
  $('#nav-agent').onclick=()=>{setPage('assistant');$('.ai-mode-tabs button:last-child')?.click();$('#nav-agent').classList.add('active');$('#nav-agent').setAttribute('aria-current','page');$('#nav-assistant').classList.remove('active');$('#nav-assistant').removeAttribute('aria-current');$('#page-title').textContent='Агент кода';};
  for(const name of ['docs','history'])$('#nav-'+name).onclick=()=>{setPage(name);if(name==='history')loadHistory();};
  $('#rail-changes').addEventListener('click',()=>{setPage('code');$('#changes-panel').hidden=false;$('#rail-changes').classList.add('active');});
  $('#ai-close').addEventListener('click',()=>setPage('code'));
  $('#docs-open-configuration').onclick=$('#dashboard-browse-code').onclick=()=>setPage('code');
  $('#home-all-history').onclick=()=>{setPage('history');loadHistory();};
  document.addEventListener('athena-open-code',()=>setPage('code'));
  document.addEventListener('athena-project-changed',()=>{latestSources=[];refresh();loadHistory();});
  $('#dashboard-model-settings').onclick=()=>$('#ai-settings-open').click();
  $('#configuration-switch').onchange=e=>{if(e.target.value&&e.target.value!==project?.root)openProject(e.target.value);};
  const settings=document.createElement('dialog');settings.id='workspace-settings-menu';
  settings.innerHTML='<div class="dialog-title"><h2>Настройки</h2><button type="button" aria-label="Закрыть настройки рабочего пространства">×</button></div><p class="muted">Подключение модели и параметры рабочего места</p><button type="button" data-target="ai-settings-open">Подключение AI-модели <span>↗</span></button><button type="button" data-target="index-settings-open">Индексация проекта <span>↗</span></button><button type="button" data-target="auto-settings">Автообновление конфигурации <span>↗</span></button><a href="/setup">Настройка рабочего места <span>↗</span></a>';
  document.body.append(settings);settings.querySelector('.dialog-title button').onclick=()=>settings.close();
  settings.addEventListener('click',e=>{const b=e.target.closest('[data-target]');if(b){settings.close();$('#'+b.dataset.target).click();}});
  $('#nav-settings').onclick=()=>settings.showModal();
  function saveHistory(){try{localStorage.setItem(historyKey,JSON.stringify(history.slice(0,30)));}catch{}}
  function currentHistory(){return history.filter(x=>x.project===projectKey()).sort((a,b)=>b.time-a.time);}
  function renderHistory(){
    for(const [id,limit] of [['home-recent-questions',2],['dashboard-history-list',30]]){
      const list=$('#'+id);list.replaceChildren();const rows=currentHistory().slice(0,limit);
      if(!rows.length){const empty=document.createElement('p');empty.className='dashboard-empty';empty.textContent=project?'Здесь появятся ваши вопросы по этой конфигурации.':'Откройте проект, чтобы задавать вопросы по конфигурации.';list.append(empty);continue;}
      for(const item of rows){const b=document.createElement('button');b.type='button';b.className='question-row';const text=document.createElement('span'),name=document.createElement('strong'),caption=document.createElement('small'),arrow=document.createElement('span');name.textContent=item.question;caption.textContent=new Date(item.time).toLocaleString('ru',{day:'numeric',month:'short',hour:'2-digit',minute:'2-digit'})+' · '+(item.engine==='agent'?'Исследование проекта':'Метаданные конфигурации');text.append(name,caption);arrow.textContent='›';arrow.className='question-arrow';b.append(text,arrow);b.onclick=()=>openHistory(item);list.append(b);}
    }
  }
  async function openHistory(item){
    if(aiJob){toast('Дождитесь завершения текущего вопроса.');return;}
    setPage('assistant');$('#ai-question').value=item.question;
    try{const job=await api((item.engine==='agent'?'agent/':'ai/')+'job?id='+encodeURIComponent(item.id));if(item.project!==projectKey())return;renderAIJob(item.engine==='agent'?questionAgentView(job):job);}
    catch{$('#ai-response').hidden=true;toast('Вопрос выбран. Нажмите «Спросить», чтобы получить новый ответ.');}
  }
  async function loadHistory(){
    const key=projectKey();if(!key){renderHistory();return;}
    try{const status=await api('agent/status');if(key!==projectKey())return;for(const job of status.jobs){if(job.context.project!==key||job.mode!=='analyze'||job.status==='running')continue;if(!history.some(x=>x.id===job.id))history.push({id:job.id,engine:'agent',question:job.goal,project:key,time:job.created*1000});}history.sort((a,b)=>b.time-a.time);history=history.slice(0,30);saveHistory();}catch{}
    renderHistory();
  }
  function sourceLinks(){
    const links=$('#dashboard-source-links');links.replaceChildren();
    for(const source of latestSources.slice(0,4)){const b=aiSourceButton(source,source.name);b.className='dashboard-source-link';const path=document.createElement('small');path.textContent=source.path;b.append(path);links.append(b);}
    if(!latestSources.length){const p=document.createElement('p');p.textContent=project?'Здесь появятся источники ответа помощника. Все объекты доступны в разделе «Код».':'Исходники появятся после открытия конфигурации.';links.append(p);}
  }
  document.addEventListener('athena-ai-rendered',e=>{
    const job=e.detail;latestSources=job.sources||[];sourceLinks();
    if(job.status==='running'||(!job.answer&&!job.goal&&!job.question))return;
    const key=projectKey();const item={id:job.id,engine:job.kind==='agent'?'agent':'ai',question:job.question||job.goal||$('#ai-question').value,project:key,time:(job.created||Date.now()/1000)*1000};
    if(!history.some(x=>x.id===item.id)){history.unshift(item);history=history.slice(0,30);saveHistory();renderHistory();}
  });
  async function refresh(){
    if(refreshing)return;refreshing=true;
    try{
      const results=await Promise.allSettled([api('knowledge'),api('ai/settings'),api('state')]);
      const root=project&&nodes.get(project.configuration);$('#dashboard-project-name').textContent=root?.name||'Конфигурация не открыта';$('#dashboard-project-subtitle').textContent=project?`${plural(project.stats.objects,['объект','объекта','объектов'])} · ${plural(project.stats.modules,['модуль','модуля','модулей'])}`:'Выберите файловую выгрузку';
      const select=$('#configuration-switch');select.replaceChildren();const paths=new Set(project?[project.root]:[]);if(results[2].status==='fulfilled')for(const path of results[2].value.recent||[])paths.add(path);
      if(!paths.size){const o=document.createElement('option');o.textContent='Конфигурация не открыта';o.value='';select.append(o);}
      for(const path of paths){const o=document.createElement('option');o.value=path;o.textContent=path===project?.root?(root?.name||'Текущая конфигурация'):path.split(/[\\/]/).slice(-2).join(' / ');o.title=path;select.append(o);}if(project)select.value=project.root;
      if(results[0].status==='fulfilled'){
        const k=results[0].value,semantic=k.semantic;$('#dashboard-index-status').textContent=k.ready?'Основной индекс готов':'Ожидает индексации';$('#dashboard-index-dot').classList.toggle('ready',!!k.ready);$('#dashboard-index-progress').style.width=k.ready?'100%':'0%';
        $('#dashboard-index-details').textContent=k.ready?`${plural(k.modules,['модуль','модуля','модулей'])} · ${plural(k.symbols,['метод','метода','методов'])}`:'Откройте проект и дождитесь подготовки.';
        if(semantic?.enabled&&semantic.total)$('#dashboard-index-details').textContent+=` · эмбеддинги ${semantic.indexed||0}/${semantic.total}`;
      }else{$('#dashboard-index-status').textContent='Нет связи с индексом';$('#dashboard-index-dot').classList.remove('ready');}
      if(results[1].status==='fulfilled'){
        const a=results[1].value,p=a.profiles[a.provider];$('#dashboard-model-status').textContent=p.model||'Модель не выбрана';$('#dashboard-model-dot').classList.toggle('ready',!!p.model&&(p.local||p.has_api_key));
        $('#dashboard-model-details').textContent=!p.model?'Подключите модель для ответов по коду.':p.local?'Локальное подключение':p.has_api_key?'API · ключ в памяти':'API · требуется ключ';
        $('#dashboard-local-details').textContent=p.local?'Индекс и ответы обрабатываются локально.':p.allow_remote_code?'Индекс локально. Для ответов выбран внешний API.':'Локальный индекс. Отправка кода внешней модели выключена.';
      }else{$('#dashboard-model-status').textContent='Нет связи с настройками';$('#dashboard-model-dot').classList.remove('ready');}
      renderHistory();sourceLinks();
    }finally{refreshing=false;}
  }
  document.addEventListener('visibilitychange',()=>{if(!document.hidden)refresh();});
  setInterval(()=>{if(!document.hidden&&page!=='code')refresh();},15000);
  setPage('home');refresh();loadHistory();
})();


// Collapsible workspace panels retain the existing tree, selection and scroll.
(()=>{
  const rail=$('.activity-rail'),tree=$('.metadata-sidebar');
  let state={navCollapsed:false,treeCollapsed:false},savedLayout=false;
  try{const raw=localStorage.getItem('athena-panel-layout');savedLayout=!!raw;const saved=JSON.parse(raw||'{}');if(saved&&typeof saved==='object')for(const key of Object.keys(state))if(typeof saved[key]==='boolean')state[key]=saved[key];}catch{}
  if(!savedLayout&&matchMedia('(max-width:620px)').matches)state.navCollapsed=true;
  for(const button of rail.querySelectorAll('button')){
    if(!button.title)button.title=button.textContent.trim();
    for(const node of [...button.childNodes])if(node.nodeType===Node.TEXT_NODE&&node.textContent.trim()){const label=document.createElement('span');label.className='nav-label';label.textContent=node.textContent;node.replaceWith(label);}
    const label=button.querySelector('.nav-label')?.textContent.trim()||button.title;button.setAttribute('aria-label',label);button.title=label;
  }
  rail.querySelector('.nav-caption')?.remove();
  const railHeader=document.createElement('div');railHeader.className='rail-panel-heading';
  railHeader.innerHTML='<span>НАВИГАЦИЯ</span><button type="button" id="collapse-navigation" aria-controls="workspace-navigation" title="Свернуть меню"><svg viewBox="0 0 20 20" aria-hidden="true"><path d="m12 5-5 5 5 5"/></svg></button>';
  rail.id='workspace-navigation';rail.prepend(railHeader);
  const collapse=document.createElement('button');collapse.type='button';collapse.id='collapse-tree';collapse.title='Свернуть дерево конфигурации';collapse.setAttribute('aria-label','Свернуть дерево конфигурации');collapse.setAttribute('aria-controls','configuration-panel');collapse.innerHTML='<svg viewBox="0 0 20 20" aria-hidden="true"><path d="m12 5-5 5 5 5"/></svg>';
  tree.id='configuration-panel';tree.querySelector('.aside-title').append(collapse);
  const toggle=document.createElement('button');toggle.type='button';toggle.id='toggle-configuration-panel';toggle.setAttribute('aria-controls',tree.id);
  toggle.innerHTML='<svg viewBox="0 0 20 20" aria-hidden="true"><rect x="3" y="3" width="14" height="14" rx="2"/><path d="M8 3v14M11 7h3M11 10h3M11 13h3"/></svg><span>Объекты</span>';
  $('#page-title').after(toggle);
  function render(){
    document.body.dataset.navCollapsed=String(state.navCollapsed);document.body.dataset.treeCollapsed=String(state.treeCollapsed);
    const navButton=$('#collapse-navigation');navButton.title=state.navCollapsed?'Развернуть меню':'Свернуть меню';navButton.setAttribute('aria-label',navButton.title);navButton.setAttribute('aria-expanded',String(!state.navCollapsed));
    toggle.title=state.treeCollapsed?'Показать дерево конфигурации':'Скрыть дерево конфигурации';toggle.setAttribute('aria-label',toggle.title);toggle.setAttribute('aria-expanded',String(!state.treeCollapsed));
    collapse.setAttribute('aria-expanded',String(!state.treeCollapsed));
    tree.inert=state.treeCollapsed;
    try{localStorage.setItem('athena-panel-layout',JSON.stringify(state));}catch{}
  }
  function toggleTree(){
    if(!state.treeCollapsed&&tree.contains(document.activeElement))toggle.focus({preventScroll:true});
    state.treeCollapsed=!state.treeCollapsed;render();
  }
  $('#collapse-navigation').onclick=()=>{state.navCollapsed=!state.navCollapsed;render();};
  collapse.onclick=toggleTree;toggle.onclick=toggleTree;
  document.addEventListener('keydown',e=>{if((e.ctrlKey||e.metaKey)&&e.key==='\\'){e.preventDefault();state.navCollapsed=!state.navCollapsed;render();}});
  // Hiding a panel never recreates its controls or discards unsaved code.
  render();
})();
