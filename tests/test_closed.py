import sys, json; sys.argv=['x','--dry']; sys.path.insert(0,''+__import__("os").path.dirname(__import__("os").path.dirname(__import__("os").path.abspath(__file__)))+'')
import scanner as S, pandas as pd, numpy as np
U=lambda s: pd.Timestamp(s, tz="UTC")
ok=0
def chk(c,msg):
    global ok; assert c, msg; ok+=1
# 15m boundaries
idx=pd.date_range("2026-10-20 13:00",periods=4,freq="15min",tz="UTC")
m=pd.DataFrame({"Close":range(4)},index=idx); h=pd.DataFrame({"Close":[1]},index=[U("2026-10-20 09:00")])
for now,last in [("13:45","13:30"),("13:46","13:30"),("13:52:30","13:30"),("14:00","13:45"),("14:01","13:45"),("13:59:59","13:30")]:
    _,mm=S.closed_only(h,m,U("2026-10-20 "+now)); chk(mm.index[-1]==U("2026-10-20 "+last),f"15m {now}")
# 4H close times, Athens buckets, DST Oct 25 2026 (EEST->EET at 04:00 local) & Mar 29 2026
cases={"2026-10-20 13:00":"2026-10-20 17:00","2026-10-24 21:00":"2026-10-25 02:00",  # bucket 00-04 local on fall-back day = 5h
       "2026-10-25 02:00":"2026-10-25 06:00","2026-03-28 22:00":"2026-03-29 01:00"}  # spring-forward bucket = 3h
for st,cl in cases.items(): chk(S.h4_close(U(st))==U(cl),f"h4close {st} got {S.h4_close(U(st))}")
# to_h4 on DST day: buckets keyed correctly
h1=pd.DataFrame({c:1.0 for c in "Open High Low Close Volume".split()},index=pd.date_range("2026-10-24 20:00","2026-10-25 08:00",freq="h",tz="UTC"))
g=S.to_h4(h1); chk(list(g.index[:3])==[U("2026-10-24 17:00"),U("2026-10-24 21:00"),U("2026-10-25 02:00")],f"to_h4 dst {list(g.index)}")
# 4H selection: bar 13:00 closes 17:00; used only once a 15m bar starting >=17:00 is closed (backtest rule)
h4=pd.DataFrame({"Close":[1,2]},index=[U("2026-10-20 09:00"),U("2026-10-20 13:00")])
m=pd.DataFrame({"Close":0},index=pd.date_range("2026-10-20 16:00","2026-10-20 17:30",freq="15min",tz="UTC"))
for now,last in [("17:00","09:00"),("17:01","09:00"),("17:15","13:00"),("16:49","09:00")]:
    hh,_=S.closed_only(h4,m,U("2026-10-20 "+now)); chk(hh.index[-1]==U("2026-10-20 "+last),f"4h {now}")
# weekend: Sat with Friday data -> all bars closed, age >1h (watch skips); fx_closed
chk(S.fx_closed(U("2026-10-24 12:00").to_pydatetime()),"sat closed"); chk(not S.fx_closed(U("2026-10-25 21:30").to_pydatetime()),"sun 17:30NY open")
chk(S.fx_closed(U("2026-10-25 20:30").to_pydatetime()),"sun 16:30NY closed (post-DST US still EDT)")
# trade time line parse compat
t=json.loads('{"time":"2026-10-20 16:49:21.343080+03:00"}'); chk(f"{pd.Timestamp(t['time']).tz_convert(S.TZ):%Y-%m-%d %H:%M}"=="2026-10-20 16:49","time fmt")
print("UNIT OK",ok)
