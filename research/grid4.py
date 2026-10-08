import sys, statistics; sys.path.insert(0,'research')
import eval2
from multiprocessing import Pool
SPLIT="2025-01-01"
def rep(over,res):
    tr=[x for x in res if x[1] and x[0]<SPLIT]; te=[x for x in res if x[1] and x[0]>=SPLIT]
    f=lambda r: f"n={len(r):3d} mean={statistics.mean(x[2] for x in r)*100:5.2f} med={statistics.median(x[2] for x in r)*100:5.2f} P>8={sum(x[2]>.08 for x in r)/len(r)*100:4.1f} P<-5={sum(x[2]<-.05 for x in r)/len(r)*100:4.1f} p10={sorted(x[2] for x in r)[len(r)//10]*100:5.1f}"
    print(f"{str(over):52s} TRAIN {f(tr)} | TEST {f(te)}")
def run(variants,L=16,step=2):
    with Pool(8,initializer=eval2.init,initargs=(eval2.FETCH42,)) as p:
        out=p.map(eval2.run_variant,[("research/agent_v4.py",v,L,step,300) for v in variants])
    for o,r in out: rep(o,r)
if __name__=="__main__":
    C={'REG200':True,'QQQ_VOL_OFF':0.25,'HOT_VOL':0.24}
    V=[{**C,'HOT_R21':0.02},{**C,'HOT_R21':0.0},{**C,'HOT_R21':-1.0}]
    run(V); sys.exit()
    for e in (0.06,0.12): V.append({"EXT_MAX":e})
    for e in (0.06,0.12): V.append({"R5_MAX":e})
    for w in (0.3,0.6): V.append({"RS_W":w})
    for w in (0.3,0.6): V.append({"HI_W":w})
    for w in ("equal","score"): V.append({"WEIGHTING":w})
    for c in ("TQQQ","SOXL"): V.append({"LEV_CHOICE":c})
    V.append({"MIN_R21":0.05}); V.append({"VOL_PEN":0.0,"W21":0.5,"W63":0.3,"W126":0.2})
    run(V)
