import sys, statistics
sys.path.insert(0,'research')
from multiprocessing import Pool
import bt, eval2, compare
def make(alpha):
    ax=bt.load_agent(bt.HERE/"agent.py"); pv=bt.load_agent(bt.HERE/"research/prev/agent.py")
    orig=ax.targets
    def blended(ms, held):
        wa=orig(ms, held) if alpha>0 else {}
        wp=pv.target_weights(ms) if alpha<1 else {}
        w={}
        for t,x in wa.items(): w[t]=w.get(t,0)+alpha*x
        for t,x in wp.items(): w[t]=w.get(t,0)+(1-alpha)*x
        g=sum(x*ax.BETA.get(t,1.0) for t,x in w.items())
        if g>ax.P["GROSS_MAX"]: w={t:x*ax.P["GROSS_MAX"]/g for t,x in w.items()}
        return w
    ax.targets=blended
    return ax.decide
ALPHAS=(0.0,0.25,0.5,0.75,1.0)
def job(a):
    alpha,L=a; S=eval2.SIM; res=[]
    for s in range(300,len(S.dates)-L+1,2):
        r=S.run(lambda: make(alpha),s,L); res.append((S.dates[s],eval2.FLAGS.get(S.dates[s-1],False),r["ret"],r["mdd"]))
    return alpha,L,res
if __name__=="__main__":
    compare.init(0); S=eval2.SIM
    print("== Round 2 windows: blend weight on Apex (0=Trendline, 1=Apex) ==")
    print("start        "+"  ".join(f"a={a:4.2f}" for a in ALPHAS))
    for st_ in ("2026-07-07","2026-08-26","2026-09-08","2026-09-30"):
        i=S.dates.index(st_); row=[]
        for a in ALPHAS:
            r=S.run(lambda: make(a),i,len(S.dates)-i); row.append(f"{r['ret']*100:5.2f}%/{r['mdd']*100:3.1f}")
        print(st_,"  ".join(row),"  (ret/maxDD)")
    with Pool(8,initializer=compare.init,initargs=(0,)) as p:
        out=p.map(job,[(a,L) for L in (16,30) for a in ALPHAS])
    for L in (16,30):
        print(f"== rolling {L}-session windows ==")
        for alpha,l,res in out:
            if l!=L: continue
            tr=[x for x in res if x[1] and x[0]<"2025-01-01"]; te=[x for x in res if x[1] and x[0]>="2025-01-01"]
            print(f"a={alpha:4.2f} ALL {compare.st(res)}\n       HOT<2025 {compare.st(tr)}\n       HOT2025+ {compare.st(te)}")
