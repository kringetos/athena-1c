import json
import unittest
from athena.agent_memory import ToolMemory

class MemoryTests(unittest.TestCase):
    def test_compaction_keeps_search_locations_and_real_code_over_inventory(self):
        memory=ToolMemory()
        rows=[{'id':str(i),'name':'Объект'+str(i),'kind':'Document','child_counts':{'Attribute':i},'files':[]} for i in range(30)]
        args={'kinds':['Document']}
        memory.remember(memory.key('list_objects',args),'list_objects',args,{'ok':True,'result':{'items':rows,'total':30,'next_offset':None}})
        hit={'id':'method-id','name':'СуммаЗаказа','signature':'СуммаЗаказа(Товары)',
             'location':{'path':'CommonModules/Расчёт/Ext/Module.bsl','file_id':'file-id','line':7},
             'code_excerpt':{'start_line':8,'end_line':8,'content':'Сумма = Цена * Количество;','truncated':False}}
        args={'query':'Сумма','scope':'code'}
        memory.remember(memory.key('search',args),'search',args,{'ok':True,'result':{'query':'Сумма','groups':{'code':[hit]},'snapshot':'snapshot'}})
        message=memory.message(1600)
        self.assertTrue(message)
        payload=json.loads(message[0]['content']);search=next(r for r in payload['athena_verified_observations'] if r['tool']=='search')
        actual=search['result']['groups']['code'][0]
        self.assertEqual(actual['location']['file_id'],'file-id')
        self.assertEqual(actual['code_excerpt']['content'],'Сумма = Цена * Количество;')
        self.assertGreater(payload['omitted_observations'],0)

    def test_empty_search_stays_explicit_and_read_excerpt_survives(self):
        memory=ToolMemory()
        memory.remember('empty','search',{'query':'НеСуществует'},{'ok':True,'result':{'query':'НеСуществует','groups':{'code':[]}}})
        memory.remember('read','read_module',{'file_id':'file'},{'ok':True,'result':{'path':'Module.bsl','start_line':10,'content':'Сумма = 12;','hash':'hash'}})
        message=memory.message(1800)[0]['content'];payload=json.loads(message)
        self.assertEqual(payload['athena_verified_observations'][0]['result']['groups']['code'],[])
        self.assertIn('Сумма = 12;',message)
