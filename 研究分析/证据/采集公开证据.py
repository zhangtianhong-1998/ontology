from pathlib import Path
import urllib.request,json,concurrent.futures,subprocess,hashlib,datetime
base=Path(__file__).parent
papers={
'E01_Text2Onto':'https://publikationen.bibliothek.kit.edu/1000018084',
'E02_OntoLearn_Reloaded':'https://aclanthology.org/J13-3007.pdf',
'E03_DL_Learner':'https://jens-lehmann.org/files/2016/jws_dllearner.pdf',
'E04_EDC':'https://aclanthology.org/2024.emnlp-main.548.pdf',
'E05_iText2KG':'https://arxiv.org/pdf/2409.03284v1',
'E06_AutoSchemaKG':'https://arxiv.org/pdf/2505.23628v1',
'E07_KGGen':'https://proceedings.neurips.cc/paper_files/paper/2025/file/2b368455e832d2b1a60bcad8c4c6481f-Paper-Conference.pdf',
'E08_SPIRES':'https://arxiv.org/pdf/2304.02711',
'E09_AutoG':'https://proceedings.iclr.cc/paper_files/paper/2025/file/c01dfe01b97aadc10fc16a201faa80d8-Paper-Conference.pdf',
'E10_OntoLearner':'https://arxiv.org/pdf/2607.01977v1',
'E11_DL_Survey':'https://arxiv.org/pdf/2104.01193',
'E12_RDB_RAG_Comparison':'https://arxiv.org/pdf/2511.05991',
'E13_Self_Demonstrations':'https://arxiv.org/pdf/2609.13776',
}
repos=['HKUST-KnowComp/AutoSchemaKG','clear-nus/edc','AuvaLab/itext2kg','stair-lab/kg-gen','monarch-initiative/ontogpt','dl-learner/DL-Learner','dice-group/Ontolearn','ontop/ontop','megagonlabs/doduo','megagonlabs/sato','sciknoworg/OntoLearner','amazon-science/Automatic-Table-to-Graph-Generation','RDFLib/pySHACL']
def get(url):
 req=urllib.request.Request(url,headers={'User-Agent':'Ontology-Evidence-Research/1.0'})
 with urllib.request.urlopen(req,timeout=60) as r:return r.read(),r.geturl()
def paper(item):
 key,url=item
 try:
  data,final=get(url); ext='.pdf' if data[:4]==b'%PDF' else '.html'; path=base/'扩展文献'/(key+ext);path.write_bytes(data)
  entry={'id':key,'url':url,'resolved_url':final,'file':str(path),'sha256':hashlib.sha256(data).hexdigest(),'bytes':len(data),'retrieved':'2026-09-20'}
  if ext=='.pdf':
   out=path.with_suffix('.txt');subprocess.run(['pdftotext','-layout',str(path),str(out)],check=True,stderr=subprocess.DEVNULL)
   pages=out.read_text().split('\f');pages=[p for p in pages if p.strip()];out.write_text('\n\n'.join(f'===== PDF PAGE {i} =====\n{p}' for i,p in enumerate(pages,1)));entry['pages']=len(pages)
  return entry
 except Exception as e:return {'id':key,'url':url,'error':str(e)}
def repo(name):
 try:
  data,_=get('https://api.github.com/repos/'+name);raw=json.loads(data); info={k:raw.get(k) for k in ['full_name','html_url','default_branch','license','archived','pushed_at']};info['retrieved']='2026-09-20'
  (base/'网页'/(name.replace('/','__')+'.json')).write_text(json.dumps(info,ensure_ascii=False,indent=2));return info
 except Exception as e:return {'full_name':name,'error':str(e)}
with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
 p=list(ex.map(paper,papers.items()));r=list(ex.map(repo,repos))
(base/'扩展文献清单.json').write_text(json.dumps(p,ensure_ascii=False,indent=2))
(base/'开源核验.json').write_text(json.dumps(r,ensure_ascii=False,indent=2))
for i in p: print(i['id'],i.get('pages'),i.get('error','OK'))
for i in r: print(i['full_name'],(i.get('license') or {}).get('spdx_id'),i.get('error',''))
