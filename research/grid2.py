import sys; sys.path.insert(0,'research')
from eval2 import *
if __name__=='__main__':
    out=main("research/agent_v2.py",[{}, {"LEV_SHARE":0.0}, {"K":4}, {"CRASH_3D":-1.0}],step=2)
    over,res=out[0]
    import collections
    by=collections.defaultdict(list)
    for d,hot,r,m in res:
        if hot: by[d[:4]].append(r)
    for y,v in sorted(by.items()): print(y,len(v),f"mean {statistics.mean(v)*100:5.2f}  med {statistics.median(v)*100:5.2f} P>8 {sum(x>0.08 for x in v)/len(v)*100:4.1f}")
