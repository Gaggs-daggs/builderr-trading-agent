import pickle, yfinance as yf
names="RELIANCE TCS HDFCBANK ICICIBANK INFY BHARTIARTL ITC SBIN LT HINDUNILVR KOTAKBANK AXISBANK BAJFINANCE MARUTI SUNPHARMA TITAN ULTRACEMCO ASIANPAINT NTPC ONGC POWERGRID TATASTEEL TATAMOTORS M&M ADANIENT ADANIPORTS WIPRO HCLTECH TECHM JSWSTEEL COALINDIA NESTLEIND BAJAJFINSV GRASIM HINDALCO CIPLA DRREDDY EICHERMOT BPCL HEROMOTOCO INDUSINDBK BRITANNIA APOLLOHOSP TRENT SHRIRAMFIN BEL HAL ETERNAL ZOMATO DLF IRFC PFC RECLTD TVSMOTOR POLYCAB DIXON BHEL SUZLON VBL JIOFIN BAJAJ-AUTO SBILIFE HDFCLIFE ADANIPOWER VEDL CHOLAFIN MAZDOCK COCHINSHIP BDL".split()
tick=[n+".NS" for n in names]+["NIFTYBEES.NS","^NSEI"]
bars={}
for t in tick:
    try: df=yf.Ticker(t).history(period="7y",interval="1d",auto_adjust=True).dropna()
    except Exception as e: print("fail",t,e); continue
    if len(df)<300: print("short/none",t,len(df)); continue
    bars[t]=[{"ts":i.strftime("%Y-%m-%d"),"open":float(r.Open),"high":float(r.High),"low":float(r.Low),"close":float(r.Close),"volume":float(r.Volume)} for i,r in df.iterrows()]
pickle.dump(bars,open('research/bars_nse.pkl','wb'))
print(len(bars),'ok'); print(bars['^NSEI'][-1]['ts'],bars['RELIANCE.NS'][0]['ts'])
