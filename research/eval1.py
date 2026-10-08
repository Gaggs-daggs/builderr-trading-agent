import sys, time
sys.path.insert(0,'research')
from bt import *
bars = load_bars(set(FETCH42))
sim = Sim(bars)
first = 300
def bh(t):
    def f():
        done=[False]
        def d(ms,ps,cash):
            if done[0]: return []
            done[0]=True; p=ms[t][-1]["close"]; return [{"ticker":t,"side":"buy","quantity":int(cash*0.995//p)}]
        return d
    return f
cand = {
 "QQQ b&h": bh("QQQ"),
 "starter agent.py": lambda: load_agent(HERE/"agent.py").decide,
 "v1": lambda: load_agent(HERE/"research"/"agent_v1.py").decide,
}
# sanity: Jul 7 start vs leaderboard (QQQ +5.97%)
s0 = sim.dates.index("2026-07-07")
for n,f in cand.items():
    r = sim.run(f, s0, 200); print(f"{n:20s} since Jul7: {r['ret']*100:6.2f}%  mdd {r['mdd']*100:5.1f}% trades {r['trades']}")
for L in (16,):
    print("== rolling",L,"day windows ==")
    for n,f in cand.items():
        t=time.time(); res=rolling(sim,f,L,first,step=3); summarize(res,n); 
