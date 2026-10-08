import sys, statistics, importlib.util
sys.path.insert(0,'research')
from multiprocessing import Pool
import bt, eval2
PATHS={"NEW (Apex)":"agent.py","PREV (Trendline)":"research/prev/agent.py"}
def init(_):
    eval2.SIM=bt.Sim(bt.load_bars(set(bt.FETCH42))); eval2.FLAGS=eval2.hot_flags(eval2.SIM)
def job(a):
    name,L=a; m=bt.load_agent(bt.HERE/PATHS[name]); S=eval2.SIM; res=[]
    for s in range(300,len(S.dates)-L+1,2):
        r=S.run(lambda: m.decide,s,L); res.append((S.dates[s],eval2.FLAGS.get(S.dates[s-1],False),r["ret"],r["mdd"]))
    return name,L,res
def st(r):
    if not r: return "n=0"
    x=sorted(v[2] for v in r); n=len(x)
    return f"n={n:3d} mean={statistics.mean(x)*100:5.2f} med={statistics.median(x)*100:5.2f} p10={x[n//10]*100:5.1f} worst={x[0]*100:5.1f} P>8={sum(v>.08 for v in x)/n*100:4.1f} P<-5={sum(v<-.05 for v in x)/n*100:4.1f}"
if __name__=="__main__":
    init(0); S=eval2.SIM
    print("== Round 2 style windows (from start to Oct 7) ==")
    for st_ in ("2026-07-07","2026-08-05","2026-08-26","2026-09-08","2026-09-30"):
        i=S.dates.index(st_); row=[]
        for n,p in PATHS.items():
            m=bt.load_agent(bt.HERE/p); r=S.run(lambda: m.decide,i,len(S.dates)-i); row.append(f"{n} {r['ret']*100:6.2f}% (mdd {r['mdd']*100:4.1f}, {r['trades']} tr)")
        print(st_," | ".join(row))
    with Pool(8,initializer=init,initargs=(0,)) as p:
        out=p.map(job,[(n,L) for L in (16,30) for n in PATHS])
    for L in (16,30):
        print(f"== rolling {L}-session windows ==")
        for name,l,res in out:
            if l!=L: continue
            tr=[x for x in res if x[1] and x[0]<"2025-01-01"]; te=[x for x in res if x[1] and x[0]>="2025-01-01"]
            print(f"{name:18s} ALL  {st(res)}\n{'':18s} HOT<2025 {st(tr)}\n{'':18s} HOT2025+ {st(te)}")
