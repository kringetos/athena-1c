"""Compare real models on an isolated synthetic project: python -m athena.agent_eval."""
import argparse
import json
import shutil
import tempfile
import re
import urllib.request
import urllib.error
from pathlib import Path
from unittest.mock import patch
from athena.ai import DEFAULTS, local_url

ROOT=Path(__file__).resolve().parent.parent
CASES=[
    {'id':'amount','goal':'Найди расчёт суммы строки и итога заказа. Укажи формулы, методы, пути и строки; проверь, где итог вызывается перед записью.',
     'expected':['СуммаЗаказа','ПередЗаписью','ТоварыКоличествоПриИзменении'],
     'facts':['Количество','Цена','СуммаДокумента']},
    {'id':'registers','goal':'Найди чтение и запись регистра ОстаткиТоваров. Укажи методы, операции и строки кода, объясни что читается и что записывается.',
     'expected':['ПрочитатьОстатки','ЗаписатьОстатки'],'facts':['ОстаткиТоваров','Прочитать','Записать']},
    {'id':'form','goal':'Найди обработчик изменения количества в форме заказа. Покажи формулу, откуда берётся текущая строка, путь и строки кода.',
     'expected':['ТоварыКоличествоПриИзменении'],'facts':['ТекущиеДанные','Количество','Цена']},
]
REGISTER_CODE='''
// Чтение регистра накопления ОстаткиТоваров.
Функция ПрочитатьОстатки() Экспорт
    Набор = РегистрыНакопления.ОстаткиТоваров.СоздатьНаборЗаписей();
    Набор.Прочитать();
    Возврат Набор;
КонецФункции

// Запись регистра накопления ОстаткиТоваров.
Процедура ЗаписатьОстатки(Набор) Экспорт
    Набор.Записать();
КонецПроцедуры
'''


def score(case, job, symbols):
    """Transparent checks of named evidence; these do not certify arbitrary prose."""
    answer=job.get('answer','').casefold()
    named=[name for name in case['expected'] if name.casefold() in answer]
    facts=[fact for fact in case['facts'] if fact.casefold() in answer]
    ranges=list(job.get('coverage',{}).get('read_ranges',[]))
    for step in job['steps']:
        if not step['ok']:continue
        for source in step.get('diagnostics',{}).get('sources',[]):
            if source.get('evidence_range'):
                ranges.append({**source['evidence_range'],'file_id':source.get('location',{}).get('file_id')})
    evidence=[];excerpts=[];cited=[]
    for symbol in symbols:
        if symbol['name'] not in case['expected']:continue
        delivered=[]
        for window in ranges:
            if window.get('file_id')!=symbol['file_id']:continue
            start=max(window.get('start_line',1),symbol['start_line']);end=min(window.get('end_line',0),symbol['end_line'])
            if start>end:continue
            lines=symbol['code'].splitlines(keepends=True)[start-symbol['start_line']:end-symbol['start_line']+1]
            if lines and end==window.get('next_line') and window.get('next_column'):lines[-1]=lines[-1][:window['next_column']]
            if lines and start==window.get('start_line'):lines[0]=lines[0][window.get('start_column',0):]
            delivered.append(''.join(lines))
        if any(delivered):
            evidence.append(symbol['name']);excerpts.extend(delivered)
            if symbol['name'].casefold() in answer and symbol['path'].casefold() in answer and any(re.search(r'(?<!\d)'+str(line)+r'(?!\d)',answer) for line in range(symbol['start_line'],symbol['end_line']+1)):cited.append(symbol['name'])
    supported=[fact for fact in case['facts'] if any(fact.casefold() in text.casefold() for text in excerpts)]
    return {'evaluated':job['status']=='ready','named_methods':named,'expected_methods':case['expected'],'answer_fact_markers':facts,
            'expected_fact_markers':case['facts'],'retrieved_methods':evidence,'source_supported_markers':supported,'cited_methods':cited,
            'method_recall':len(named)/len(case['expected']),
            'passed':job['status']=='ready' and len(named)==len(case['expected']) and len(facts)==len(case['facts']) and len(supported)==len(case['facts']) and set(case['expected'])<=set(cited),
            'notice':'Проверка имён и маркеров с трассой источников; смысл, точность цитат и полнота объяснения требуют просмотра ответа.'}


def infrastructure_probe(base_url, model):
    """Diagnose local engine failures without logging arbitrary provider error bodies."""
    request=urllib.request.Request(base_url+'/api/chat',data=json.dumps({'model':model,
        'messages':[{'role':'user','content':'OK'}],'stream':False,'options':{'num_predict':1}}).encode(),headers={'Content-Type':'application/json'})
    try:
        with urllib.request.urlopen(request,timeout=10):return 'responding'
    except urllib.error.HTTPError as exc:
        body=exc.read(10000).decode('utf-8',errors='replace')
        return 'missing_llama_server_binary' if 'llama-server binary not found' in body else 'provider_http_'+str(exc.code)
    except (OSError,ValueError):return 'provider_unavailable'


def run(models, connection, max_seconds=90, case_ids=None):
    from athena.server import Workspace
    cases=[case for case in CASES if not case_ids or case['id'] in case_ids]
    results=[]
    with tempfile.TemporaryDirectory(prefix='athena-agent-eval-') as tmp:
        directory=Path(tmp);project=directory/'project'
        shutil.copytree(ROOT/'examples/demo',project)
        for target,fixture in json.loads((ROOT/'tests/fixtures/search/dataset.json').read_text(encoding='utf-8'))['sources'].items():
            shutil.copyfile(ROOT/'tests/fixtures/search'/fixture,project/target)
        module=project/'CommonModules/РасчётЗаказов/Ext/Module.bsl'
        module.write_text(module.read_text(encoding='utf-8')+REGISTER_CODE,encoding='utf-8')
        with patch('athena.server.STATE',directory/'recent.json'):
            w=Workspace()
            try:
                w.index(project,lambda *args:None)
                with w.knowledge.connect() as db:
                    symbols=[json.loads(row[0]) for row in db.execute('SELECT data FROM symbols WHERE snapshot_id=?',(w.knowledge_snapshot,))]
                for model in models:
                    w.ai.configure({'provider':'ollama',**DEFAULTS['ollama'],**connection,'model':model})
                    w.code_agent.configure({**w.code_agent.config,'max_seconds':max_seconds,'max_steps':8,'max_tool_calls':24,'context_chars':32000,'max_tokens':4096})
                    for case in cases:
                        context=w.agent_tools.context()
                        started=w.code_agent.start({'goal':case['goal'],'mode':'analyze','context':{k:context[k] for k in ('project','snapshot','workspace_revision')}})
                        worker=w.code_agent.jobs[started['id']]['_worker']
                        worker.join(max_seconds+connection.get('timeout',60)+5)
                        if worker.is_alive():
                            w.code_agent.cancel(started['id']);worker.join(connection.get('timeout',60)+5)
                            if worker.is_alive():raise RuntimeError('Транспорт не завершился после отмены; сравнение остановлено.')
                        job=w.code_agent.job(started['id'])
                        result={'model':model,'case':case['id'],'status':job['status'],'stop_reason':job.get('stop_reason'),
                                'phase':job['phase'],'error':job.get('error'),'seconds':round(job['finished']-job['created'],2),
                                'rounds':job['rounds'],'tool_calls':job['tool_calls'],'answer':job['answer'],'steps':job['steps'],
                                'usage':job['usage'],'coverage':job.get('coverage',{}),'score':score(case,job,symbols)}
                        results.append(result)
                        print(json.dumps({k:result[k] for k in ('model','case','status','seconds','rounds','tool_calls')},ensure_ascii=False),flush=True)
                    if all(row['status']=='error' and row['tool_calls']==0 for row in results if row['model']==model):
                        diagnosis=infrastructure_probe(connection['base_url'],model)
                        for row in results:
                            if row['model']==model:row['infrastructure']=diagnosis
            finally:w.close()
    return {'fixture':'examples/demo + tests/fixtures/search + synthetic register methods','models':models,
            'mechanics':'same tools, lexical index, budgets and cases; isolated workspace per evaluation',
            'results':results}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--models',nargs='+',required=True)
    parser.add_argument('--base-url',default='http://127.0.0.1:11434')
    parser.add_argument('--max-seconds',type=int,default=90)
    parser.add_argument('--cases',nargs='+',choices=[c['id'] for c in CASES])
    parser.add_argument('--output',type=Path,default=ROOT/'.athena/evaluations/agent-models.json')
    args=parser.parse_args()
    if not local_url(args.base_url):parser.error('Этот стенд передаёт синтетический код только локальному Ollama.')
    report=run(args.models,{'base_url':args.base_url,'timeout':min(args.max_seconds,120),'context_chars':32000},args.max_seconds,args.cases)
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')


if __name__=='__main__':main()
