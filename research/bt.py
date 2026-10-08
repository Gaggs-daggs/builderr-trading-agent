"""Fast re-implementation of live_runner.run_bot (same fills/caps/slippage) for rolling-window research."""
import bisect, importlib.util, math, pickle, statistics, sys
from pathlib import Path
HERE = Path(__file__).resolve().parent.parent
BETA3 = {"TQQQ","SOXL","UPRO","SPXL","TNA","FAS","TECL","LABU","CURE","DRN","UDOW","NAIL"}
BETA2 = {"QLD","SSO","DDM","ROM","UWM","AGQ"}
def beta(t): return 3.0 if t in BETA3 else 2.0 if t in BETA2 else 1.0
SE, SL, MAXW, MAXG = 0.0005, 0.0010, 0.30, 1.50
FETCH42 = ["SPY","QQQ","NVDA","MSFT","AAPL","META","AMZN","GOOGL","AVGO","AMD","MU","MRVL","NFLX","TSLA","PLTR","ORCL","CRM","JPM","V","MA","COST","LLY","SMH","XLK","XLC","XLY","XLF","XLI","XLE","XLV","XLP","XLU","XLRE","DIA","IWM","SOXX","QLD","SSO","TQQQ","SOXL","UPRO","SPXL"]

def load_bars(tickers=None):
    b = pickle.load(open(HERE/"research"/"bars.pkl","rb"))
    return {t:v for t,v in b.items() if tickers is None or t in tickers}

def load_agent(path):
    spec = importlib.util.spec_from_file_location("ag_%s"%abs(hash(str(path))), path)
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); return m

class Sim:
    def __init__(self, bars):
        self.bars = bars
        self.dates = sorted({x["ts"] for r in bars.values() for x in r})
        self.idx = {t:{x["ts"]:i for i,x in enumerate(r)} for t,r in bars.items()}
        self.tsl = {t:[x["ts"] for x in r] for t,r in bars.items()}
    def run(self, decide_factory, start, n_days, hist=260):
        """start = index into self.dates of first scored session; returns dict."""
        decide = decide_factory()
        cash = 100000.0; pos = {}; avg = {}; curve = []; trades = 0
        dates = self.dates[start:start+n_days]
        for d in dates:
            ms = {}
            for t,r in self.bars.items():
                k = bisect.bisect_left(self.tsl[t], d)   # bars strictly before d
                if k: ms[t] = r[max(0,k-hist):k]
            opn = {}; cls = {}
            for t in self.bars:
                i = self.idx[t].get(d)
                if i is not None: opn[t] = self.bars[t][i]["open"]; cls[t] = self.bars[t][i]["close"]
            prior = {t:v[-1]["close"] for t,v in ms.items()}
            ps = {"cash":cash,"positions":[{"ticker":t,"quantity":q,"avg_cost":avg.get(t,0.0)} for t,q in pos.items() if q>0],"last_prices":prior}
            try: orders = decide(ms, ps, cash) or []
            except Exception as e:
                raise
            norm=[]
            for o in orders[:100]:
                try: tk=str(o["ticker"]).strip().upper(); side=o["side"]; q=float(o["quantity"])
                except Exception: continue
                if side not in ("buy","sell") or not math.isfinite(q) or q<=0 or tk not in opn: continue
                norm.append((tk,side,q))
            for tk,side,q in sorted(norm,key=lambda x:0 if x[1]=="sell" else 1):
                px=opn[tk]; sl=SL if beta(tk)>1 else SE
                if side=="buy":
                    fill=px*(1+sl)
                    eq=max(cash+sum(pos.get(t,0)*opn.get(t,0) for t in pos),1e-9)
                    held=pos.get(tk,0.0)
                    room=max(0.0,MAXW*eq-held*fill)
                    used=sum(pos.get(t,0)*opn.get(t,0)*beta(t) for t in pos)
                    broom=max(0.0,MAXG*eq-used)
                    mn=min(cash,room,broom/beta(tk)); q=min(q,mn/fill if fill>0 else 0)
                    if q<=0: continue
                    avg[tk]=(avg.get(tk,0)*held+fill*q)/(held+q); pos[tk]=held+q; cash-=fill*q; trades+=1
                else:
                    held=pos.get(tk,0.0); q=min(q,held)
                    if q<=0: continue
                    cash+=px*(1-sl)*q; pos[tk]=held-q; trades+=1
            eq=max(cash+sum(pos.get(t,0)*cls.get(t,0) for t in pos),1e-9); curve.append(eq)
        peak=0;mdd=0
        for v in curve:
            peak=max(peak,v); mdd=max(mdd,(peak-v)/peak)
        return {"ret":curve[-1]/100000-1,"mdd":mdd,"trades":trades,"curve":curve,"dates":dates}

def rolling(sim, factory, n_days, first_idx, step=1):
    out=[]
    for s in range(first_idx, len(sim.dates)-n_days+1, step):
        out.append((sim.dates[s], sim.run(factory, s, n_days)))
    return out

def summarize(res, label=""):
    r=sorted(x[1]["ret"] for x in res); n=len(r)
    q=lambda p:r[min(n-1,int(p*n))]
    mdd=statistics.mean(x[1]["mdd"] for x in res)
    print(f"{label:28s} n={n:4d} mean={statistics.mean(r)*100:6.2f}% med={q(.5)*100:6.2f}% p10={q(.1)*100:6.2f}% p90={q(.9)*100:6.2f}% worst={r[0]*100:6.2f}% P(>8%)={sum(x>0.08 for x in r)/n*100:4.1f}% P(>0)={sum(x>0 for x in r)/n*100:4.1f}% avgMDD={mdd*100:4.1f}%")
