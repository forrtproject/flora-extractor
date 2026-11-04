"""Live probe of repository routes (#188). Isolated cache; generic client; one request at a time."""
import csv,json,os,re,sys,time,hashlib,html
WT=sys.argv[1]; SP=sys.argv[2]
os.environ['FLORA_CACHE_DIR']=f'{SP}/probe_cache'
sys.path.insert(0,WT)
import requests
from urllib.parse import urlparse,urljoin,quote
from shared.pdf_sources import _document_format,_title_check,_declared_pdf_urls,_front_matter_text,_MIN_PDF_BYTES
UA={'User-Agent':'flora-repository-probe/1.0','Accept':'*/*'}
HTTP=f'{SP}/probe_http'; DOCS=f'{SP}/probe_docs'
last={}
def get(url,timeout=30,allow=True,cache=True):
    """(kind,status,final,ctype,body). kind: ok|outage. Bodies cached on disk so reruns repeat no request."""
    key=hashlib.md5(url.encode()).hexdigest(); meta=f'{HTTP}/{key}.json'; blob=f'{HTTP}/{key}.bin'
    if cache and os.path.exists(meta):
        m=json.load(open(meta))
        if m['kind']=='ok': return m['kind'],m['status'],m['final'],m['ctype'],open(blob,'rb').read()
    h=urlparse(url).netloc.lower(); wait=1.0-(time.time()-last.get(h,0))
    if wait>0: time.sleep(wait)
    last[h]=time.time()
    try:
        r=requests.get(url,headers=UA,timeout=timeout,allow_redirects=allow,stream=True)
        body=b''
        for c in r.iter_content(65536):
            body+=c
            if len(body)>80_000_000: break
    except Exception as e:
        json.dump({'kind':'outage','err':str(e)[:200]},open(meta,'w'));
        return 'outage',0,url,str(e)[:120],b''
    last[urlparse(r.url).netloc.lower()]=time.time()
    kind='outage' if r.status_code>=500 else 'ok'
    json.dump({'kind':kind,'status':r.status_code,'final':r.url,'ctype':r.headers.get('content-type','')},open(meta,'w'))
    open(blob,'wb').write(body)
    return kind,r.status_code,r.url,r.headers.get('content-type',''),body
CH=re.compile(rb'(?i)just a moment|cf-chl|anubis|making sure you.re not a bot|captcha|awswaf|verify you are human|challenge-platform')
def classify(kind,st,body):
    if kind=='outage': return 'outage'
    if st in(401,403,429,202,503) or (CH.search(body[:20000]) and not body.startswith(b'%PDF')): return 'challenge'
    if st in(404,410): return 'gone'
    if st!=200: return f'http_{st}'
    return 'ok'
RANK={'match':5,'low':4,'no_text':3,'mismatch':2,'file_too_small':1.5,'not_doc':1,'challenge':.8,'gone':.6,'outage':.5,'no_candidates':0}
def try_doc(url,title,referer=None):
    kind,st,fin,ct,body=get(url,timeout=60)
    c=classify(kind,st,body)
    if c!='ok': return {'res':c if c in RANK else 'not_doc','doc_url':url,'note':f'{st} {ct[:30]}'}
    suf,why=_document_format(body,url)
    if not suf: return {'res':'not_doc','doc_url':url,'note':f'{why} {ct[:30]}'}
    if len(body)<_MIN_PDF_BYTES: return {'res':'file_too_small','doc_url':url,'note':str(len(body))}
    v,cov=_title_check(body,suf,title)
    p=f'{DOCS}/{hashlib.md5(url.encode()).hexdigest()}{suf}'; open(p,'wb').write(body)
    front=re.sub(r'\s+',' ',_front_matter_text(body,suf))[:500]
    return {'res':v,'doc_url':url,'cov':round(cov,2),'bytes':len(body),'suffix':suf,'front':front,'path':p}
def best(cands,title,limit=3):
    if not cands: return {'res':'no_candidates'}
    out=None
    for u in list(dict.fromkeys(cands))[:limit]:
        r=try_doc(u,title)
        if out is None or RANK[r['res']]>RANK[out['res']]: out=r
        if r['res'] in('match',): break
    return out
def page_links(page,base):
    links=[html.unescape(u) for u in _declared_pdf_urls(page)]
    for pat in (r'href=["\']([^"\']+\.pdf[^"\']*)["\']',r'href=["\']([^"\']+/bitstream/[^"\']+)["\']',r'href=["\']([^"\']*/download[^"\']*)["\']'):
        links+=[html.unescape(m) for m in re.findall(pat,page,re.I)]
    res=list(dict.fromkeys(urljoin(base,l) for l in links))
    main=[u for u in res if not re.search(r'(?i)supplement|appendix|supp_|_s\d|\.xml|\.txt',u)]
    return main or res
def platform(page,final,st):
    h=urlparse(final).netloc.lower(); pg=page[:60000]
    m=re.search(r'<meta[^>]+name=["\']generator["\'][^>]*content=["\']([^"\']+)',pg,re.I) or re.search(r'<meta[^>]+content=["\']([^"\']+)["\'][^>]*name=["\']generator',pg,re.I)
    gen=(m.group(1) if m else '')
    if re.search('dspace',gen,re.I): return gen[:20]
    if '<ds-app' in pg or 'dspace-angular' in pg: return 'DSpace 7+'
    if re.search(r'/xmlui|/jspui|dspace',final+pg[:5000],re.I): return 'DSpace (unversioned)'
    if re.search('eprints',gen+final,re.I) or 'eprints' in pg.lower()[:8000]: return 'EPrints'
    if 'bepress' in pg or 'digitalcommons' in h: return 'bepress/Digital Commons'
    if 'figshare' in h: return 'Figshare'
    if re.search('pure',gen,re.I) or '/portal/' in final: return 'Pure'
    if 'proquest' in h or 'rave' in h: return 'other: '+h
    return 'other: '+h
def handle_of(u):
    m=re.search(r'hdl\.handle\.net/(.+?)/?$',u) or re.search(r'/handle/(\d[\d.]*/[^/?#]+)',u)
    return m.group(1) if m else ''
def rest(base,hd,title):
    k,st,_,ct,b=get(f'{base}/rest/handle/{hd}?expand=bitstreams')
    c=classify(k,st,b)
    if c!='ok' : return {'res':c if c in RANK else 'no_candidates','note':f'meta {st}'}
    try: j=json.loads(b)
    except Exception: return {'res':'no_candidates','note':'not json'}
    bs=[x for x in (j.get('bitstreams') or []) if (x.get('bundleName') or 'ORIGINAL')=='ORIGINAL' and re.search(r'pdf|word|docx',(x.get('mimeType')or '')+(x.get('name')or ''),re.I)]
    return best([urljoin(base,x.get('retrieveLink') or '') for x in bs],title)|{'note2':f'rest {len(j.get("bitstreams") or [])} bs'}
def dspace7(base,hd,title):
    k,st,_,ct,b=get(f'{base}/server/api/pid/find?id=hdl:{hd}')
    c=classify(k,st,b)
    if c!='ok': return {'res':c if c in RANK else 'no_candidates','note':f'meta {st}'}
    try: j=json.loads(b); self_=j['_links']['bundles']['href']
    except Exception: return {'res':'no_candidates','note':'no item'}
    k,st,_,_,b=get(self_+'?embed=bitstreams&size=20')
    try: bun=json.loads(b)['_embedded']['bundles']
    except Exception: return {'res':'no_candidates','note':f'bundles {st}'}
    cand=[]
    for x in bun:
        if x.get('name')!='ORIGINAL': continue
        for s in x.get('_embedded',{}).get('bitstreams',{}).get('_embedded',{}).get('bitstreams',[]):
            if re.search(r'\.(pdf|docx)$',s.get('name') or '',re.I): cand.append(s['_links']['content']['href'])
    return best(cand,title)
OAIMEMO={}
def oai(base,hd,title):
    host=urlparse(base).netloc
    if base not in OAIMEMO:
        OAIMEMO[base]=None
        for path in('/oai/request','/dspace-oai/request','/server/oai/request','/oai'):
            k,st,fin,ct,b=get(f'{base}{path}?verb=Identify')
            if k=='ok' and st==200 and b'<OAI-PMH' in b:
                rid=re.search(rb'<repositoryIdentifier>([^<]+)',b); sample=re.search(rb'<sampleIdentifier>([^<]+)',b)
                k2,st2,_,_,f=get(f'{base}{path}?verb=ListMetadataFormats')
                fm=[x.decode() for x in re.findall(rb'<metadataPrefix>([^<]+)',f)]
                OAIMEMO[base]={'path':path,'rid':(rid.group(1).decode() if rid else host),'fmts':fm,'sample':sample.group(1).decode() if sample else ''}
                break
    m=OAIMEMO[base]
    if not m: return {'res':'no_candidates','note':'no oai endpoint'}
    note=f"fmts={','.join(m['fmts'][:8])}"
    for fmt in('didl','ore','mets','xoai'):
        if fmt not in m['fmts']: continue
        k,st,_,_,b=get(f"{base}{m['path']}?verb=GetRecord&metadataPrefix={fmt}&identifier=oai:{m['rid']}:{hd}")
        t=b.decode('utf8','replace')
        urls=[html.unescape(u) for u in re.findall(r'https?://[^\s"<>\']+(?:bitstream|\.pdf)[^\s"<>\']*',t)]
        urls=[u for u in urls if not re.search(r'(?i)\.(txt|jpg|xml)(\?|$)|license',u)]
        if urls: return best(urls,title)|{'note':note+f' via {fmt}'}
        if b'idDoesNotExist' in b: note+=' idDoesNotExist'; 
    return {'res':'no_candidates','note':note}
def probe_dspace(row,final,page,out):
    """Handle or EconStor target: run the DSpace routes against the resolved host."""
    hd=handle_of(row['url_r']) or handle_of(final) 
    if not hd: return
    p=urlparse(final); base=f'{p.scheme}://{p.netloc}'
    out['dspace_rest']=rest(base,hd,row['title'])
    out['dspace_oai']=oai(base,hd,row['title'])
    out['dspace7_api']=dspace7(base,hd,row['title'])
def probe_row(row):
    u=row['url_r'];t=row['title'];out={}
    k,st,fin,ct,b=get(u)
    c=classify(k,st,b); info={'landing_status':f'{c}:{st}','final':fin,'ctype':ct[:40]}
    page=b.decode('utf8','replace') if 'html' in ct.lower() else ''
    info['platform']=platform(page,fin,st) if page else ('direct-file' if b[:4]==b'%PDF' else f'none:{c}')
    # baseline: the existing row_url tier's question under this client
    if b[:4] in(b'%PDF',b'PK\x03\x04'): out['row_url']=try_doc(u,t)
    else: out['row_url']={'res':c if c!='ok' else 'not_doc','note':ct[:30]}
    # the existing scraper's question, but asked of url_r (tier 8 never asks it)
    if page and c=='ok': out['url_r_page_scrape']=best(page_links(page,fin),t)
    else: out['url_r_page_scrape']={'res':c if c in RANK else 'no_candidates'}
    g=row['group']
    if g=='handle': probe_dspace(row,fin,page,out)
    if g=='repec':
        hl=re.findall(r'https?://(?:hdl\.handle\.net/10419/\d+|[^"\']*econstor\.eu/[^"\']*handle/10419/\d+)',page)
        out['repec_followed']={'res':'no_candidates'}
        ext=[l for l in re.findall(r'href=["\'](https?://[^"\']+)',page) if not re.search(r'repec\.org|econpapers|ideas\.|oru\.se|openurl|scholar\.google',l)]
        info['repec_ext']=ext[:6]
        if hl:
            k2,st2,f2,ct2,b2=get(hl[0]); pg=b2.decode('utf8','replace')
            r2={'url_r':hl[0],'title':t}; probe_dspace(r2,f2,pg,out)
            out['repec_followed']=best(page_links(pg,f2),t)
        elif ext:
            out['repec_followed']=best([e for e in ext if re.search(r'(?i)\.pdf|download|handle|bitstream',e)],t)
    if g=='eprints':
        m=re.match(r'(https?://[^/]+)(?:/id/eprint)?/(\d+)(/.*)?$',u)
        if m and (b[:4]!=b'%PDF'):
            land=f'{m.group(1)}/{m.group(2)}/'
            if not page or c!='ok':
                k2,st2,f2,ct2,b2=get(land)
                pg=b2.decode('utf8','replace') if 'html' in ct2.lower() else ''
                if pg: info['platform']=platform(pg,f2,st2)
                out['eprints_landing']=best(page_links(pg,f2),t) if pg else {'res':classify(k2,st2,b2) if classify(k2,st2,b2) in RANK else 'no_candidates'}
            else: out['eprints_landing']=out['url_r_page_scrape']
    if g=='figshare':
        m=re.search(r'/(\d{5,})(?:/\d+)?/?$',u)
        if m:
            k2,st2,_,_,b2=get(f'https://api.figshare.com/v2/articles/{m.group(1)}')
            c2=classify(k2,st2,b2)
            if c2=='ok':
                try:
                    j=json.loads(b2); info['fs_type']=j.get('defined_type_name'); fl=j.get('files') or []
                    info['fs_files']=[(x.get('name'),x.get('mimetype'),x.get('is_link_only')) for x in fl][:6]
                    cand=[x['download_url'] for x in fl if re.search(r'\.(pdf|docx)$',x.get('name') or '',re.I) and not x.get('is_link_only')]
                    out['figshare_api']=best(cand,t)
                except Exception as e: out['figshare_api']={'res':'no_candidates','note':str(e)[:80]}
            else: out['figshare_api']={'res':c2 if c2 in RANK else 'no_candidates','note':f'api {st2}'}
        if row['doi']:
            k3,st3,_,_,b3=get(f"https://api.datacite.org/dois/{quote(row['doi'],safe='')}")
            try:
                a=json.loads(b3)['data']['attributes']; urls=[x for x in (a.get('contentUrl') or [])]
                info['dc_contentUrl']=urls[:3]
                out['datacite_existing']=best([x for x in urls if re.search(r'\.(pdf|docx)',x,re.I)],t)
            except Exception: out['datacite_existing']={'res':'no_candidates','note':f'dc {st3}'}
    return info,out
rows=json.load(open(sys.argv[3])); only=set(sys.argv[5].split(',')) if len(sys.argv)>5 else None
fp=open(sys.argv[4],'a')
done={json.loads(l)['url_r'] for l in open(sys.argv[4])} if os.path.getsize(sys.argv[4]) else set()
for i,row in enumerate(rows):
    if row['url_r'] in done or (only and row['group'] not in only): continue
    try: info,out=probe_row(row)
    except Exception as e: info,out={'crash':repr(e)[:200]},{}
    fp.write(json.dumps({**row,'info':info,'routes':out})+'\n'); fp.flush()
    print(i,row['group'],row['url_r'][:60],info.get('platform'),{k:v.get('res') for k,v in out.items()},flush=True)
