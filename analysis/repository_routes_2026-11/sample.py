"""Build the fixed-seed sample of distinct url_r per group. Read-only on data/."""
import csv,random,re,sys,json,collections as C
from urllib.parse import urlparse
csv.field_size_limit(10**9)
WT,OUT=sys.argv[1],sys.argv[2]
rows=[]
for f,t in (('data/extracted.csv','e'),('data/target_pending.csv','t')):
    for x in csv.DictReader(open(f'{WT}/{f}',encoding='utf-8-sig')):
        if (x.get('pdf_source') or '').strip(): continue
        if t=='e' and x.get('outcome')!='cannot_be_determined': continue
        x['_f']=t; rows.append(x)
def group(u):
    p=urlparse(u); h=p.netloc.lower()
    if 'figshare' in h: return 'figshare'
    if h=='hdl.handle.net': return 'handle'
    if 'repec' in h or 'econstor' in h: return 'repec'
    if 'eprints' in h or '/id/eprint/' in p.path.lower(): return 'eprints'
fr=C.defaultdict(dict)
for x in rows:
    k=group(x['url_r'])
    if k: fr[k].setdefault(x['url_r'],x)
print({k:len(v) for k,v in fr.items()},'rows',C.Counter(group(x['url_r']) for x in rows))
rng=random.Random(188)
n_h=len(fr['handle']);n_r=len(fr['repec']);k_r=round(40*n_r/(n_h+n_r));
plan={'handle':40-k_r,'repec':k_r,'figshare':40,'eprints':40}
out=[]
for k,n in plan.items():
    us=sorted(fr[k]); rng.shuffle(us)
    for u in us[:n]:
        x=fr[k][u]
        out.append({'group':k,'url_r':u,'title':x['title_r'],'doi':x['doi_r'],'work':x['oa_work_id_r'],'file':x['_f'],'link_method':x['link_method'],'evidence':(x['link_evidence'] or '')[:300]})
print(plan)
json.dump(out,open(OUT,'w'),indent=1)
print(C.Counter(o['group'] for o in out))
print(C.Counter(re.sub(r'[^a-z_ ]','',o['evidence'].lower())[:40] for o in out).most_common(8))
