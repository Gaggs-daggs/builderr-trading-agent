import sys, time, itertools, statistics, importlib.util, json
sys.path.insert(0,'research')
from multiprocessing import Pool
from bt import *
TICK = None
def hot_flags(sim):
    q=[x["close"] for x in sim.bars["QQQ"]]; s=[x["close"] for x in sim.bars["SPY"]]; ts=sim.tsl["QQQ"]
    flags={}
    for i in range(60,len(q)):
        sma=lambda x,n:sum(x[i-n+1:i+1])/n
        hot = q[i]>sma(q,50) and s[i]>sma(s,50) and q[i]>sma(q,20)>sma(q,50) and q[i]>max(q[i-19:i+1])*0.97
        flags[ts[i]]=hot
    return flags
def init(universe):
    global SIM, FLAGS, UNI
    UNI=universe
    SIM=Sim(load_bars(set(universe) if universe else None)); FLAGS=hot_flags(SIM)
def run_variant(args):
    path, over, L, step, first = args
    spec = importlib.util.spec_from_file_location("a%d"%abs(hash(json.dumps(over,sort_keys=True))), path)
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
    m.P.update(over)
    res=[]
    for s in range(first, len(SIM.dates)-L+1, step):
        prev=SIM.dates[s-1]
        r=SIM.run(lambda: m.decide, s, L)
        res.append((SIM.dates[s], FLAGS.get(prev,False), r["ret"], r["mdd"]))
    return over, res
def stats(res):
    r=sorted(x[2] for x in res); n=len(r)
    if n==0: return "n=0"
    q=lambda p:r[min(n-1,int(p*n))]
    return f"n={n:3d} mean={statistics.mean(r)*100:6.2f} med={q(.5)*100:6.2f} p10={q(.1)*100:6.2f} p90={q(.9)*100:6.2f} worst={r[0]*100:6.1f} P>5={sum(x>0.05 for x in r)/n*100:4.1f} P>8={sum(x>0.08 for x in r)/n*100:4.1f} P<-5={sum(x<-0.05 for x in r)/n*100:4.1f} mdd={statistics.mean(x[3] for x in res)*100:4.1f}"
def main(path, variants, L=16, step=2, first=300, universe=FETCH42, procs=8):
    t=time.time()
    with Pool(procs, initializer=init, initargs=(universe,)) as p:
        out=p.map(run_variant, [(path,v,L,step,first) for v in variants])
    for over,res in out:
        hot=[x for x in res if x[1]]
        print(f"{json.dumps(over):70s}\n   ALL {stats(res)}\n   HOT {stats(hot)}")
    print("elapsed",round(time.time()-t))
    return out
if __name__=="__main__":
    path=sys.argv[1]
    main(path,[{}])
