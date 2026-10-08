import sys; sys.path.insert(0,'research')
from eval2 import *
def build():
  V=[{}]
  for k in (2,3,4,5,6): V.append({"K":k})
  for l in (0.0,0.1,0.25,0.30): V.append({"LEV_SHARE":l})
  for vp in (0.0,0.25,1.0): V.append({"VOL_PEN":vp})
  for st in (0.06,0.15,1.0): V.append({"STOP":st})
  for wm in (0.20,0.29): V.append({"W_MAX":wm})
  V.append({"W63":0.2,"W21":0.6,"W126":0.2}); V.append({"W63":0.2,"W21":0.2,"W126":0.6}); V.append({"W63":0.7,"W21":0.1,"W126":0.2})
  V.append({"NEAR_HIGH":0.05}); V.append({"NEAR_HIGH":0.2})
  V.append({"HOLD_RANK":4}); V.append({"HOLD_RANK":15}); V.append({"BAND":0.06})
  return V
if __name__=='__main__':
  main("research/agent_v1.py",build(),step=2)
