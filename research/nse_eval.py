import sys, pickle, statistics, time
sys.path.insert(0,'research')
import bt, eval2
from multiprocessing import Pool
def nse_bars():
    b=pickle.load(open('research/bars_nse.pkl','rb'))
    b["QQQ"]=b.pop("NIFTYBEES.NS"); b["SPY"]=b.pop("^NSEI")
    return b
def init(_):
    bt.SE=0.0015; bt.SL=0.0015
    eval2.SIM=bt.Sim(nse_bars()); eval2.FLAGS=eval2.hot_flags(eval2.SIM)
BASE={"LEV_SHARE":0.0,"MIN_DOLLAR_VOL":2e8}
def run(L, variants, step=2):
    t=time.time()
    with Pool(8, initializer=init, initargs=(0,)) as p:
        out=p.map(eval2.run_variant,[("research/agent_v3.py",{**BASE,**v},L,step,300) for v in variants])
    for over,res in out:
        hot=[x for x in res if x[1]]
        print(f"L={L} {({k:v for k,v in over.items() if k not in BASE})}\n   ALL {eval2.stats(res)}\n   HOT {eval2.stats(hot)}")
    return out
if __name__=="__main__":
    init(0); sim=eval2.SIM
    q=[x["close"] for x in sim.bars["QQQ"]]; ts=sim.tsl["QQQ"]; fl=eval2.FLAGS
    import statistics as st
    for L in (16,22,45):
        r=[(ts[i+L-1]and q[i+L-1]/q[i-1]-1, fl.get(ts[i-1],False)) for i in range(300,len(q)-L,2)]
        allr=sorted(x[0] for x in r); hot=sorted(x[0] for x in r if x[1])
        print(f"Nifty b&h L={L}: all mean {st.mean(allr)*100:.2f} med {st.median(allr)*100:.2f} | hot n={len(hot)} mean {st.mean(hot)*100:.2f} med {st.median(hot)*100:.2f} P>8 {sum(x>.08 for x in hot)/len(hot)*100:.1f}")
    print("universe:",len(sim.bars),"tickers; dates",sim.dates[0],sim.dates[-1])
    for L in (16,22,45): run(L,[{}] if L!=22 else [{}, {"K":5}, {"K":2}, {"STOP":1.0}, {"W_MAX":0.20}])
