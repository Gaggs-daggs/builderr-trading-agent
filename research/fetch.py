import json, pickle, yfinance as yf
u=json.load(open('universe.json'))['tickers']
fetch=["SPY","QQQ","NVDA","MSFT","AAPL","META","AMZN","GOOGL","AVGO","AMD","MU","MRVL","NFLX","TSLA","PLTR","ORCL","CRM","JPM","V","MA","COST","LLY","SMH","XLK","XLC","XLY","XLF","XLI","XLE","XLV","XLP","XLU","XLRE","DIA","IWM","SOXX","QLD","SSO","TQQQ","SOXL","UPRO","SPXL"]
extra=["SNDK","LITE","WDC","STX","COHR","GLW","LRCX","AMAT","KLAC","ASML","INTC","APP","HOOD","COIN","MSTR","ARM","VRT","GEV","CRWD","PANW","GLD","TLT","TECL","USO","XOP","XBI","ARKK","SMCI","DELL","ANET","CIEN","TSM"]
tick=[t for t in dict.fromkeys(fetch+extra) if t in set(u) or t in fetch]
bars={}
for t in tick:
    try: df=yf.Ticker(t).history(period="7y",interval="1d",auto_adjust=True).dropna()
    except Exception as e: print("fail",t,e); continue
    if df.empty: continue
    bars[t]=[{"ts":i.strftime("%Y-%m-%d"),"open":float(r.Open),"high":float(r.High),"low":float(r.Low),"close":float(r.Close),"volume":float(r.Volume)} for i,r in df.iterrows()]
pickle.dump(bars,open('research/bars.pkl','wb'))
print(len(bars),'tickers', {t:len(v) for t,v in list(bars.items())[:5]}, 'missing',[t for t in tick if t not in bars])
print(bars['SPY'][0]['ts'],bars['SPY'][-1]['ts'])
