import sys, importlib.util, numpy as np, pandas as pd
sp = importlib.util.spec_from_file_location("q", "scripts/research/quantpedia_sweep.py"); q = importlib.util.module_from_spec(sp); sp.loader.exec_module(q)
IN = q.in_stocks(); IN = IN[IN.index >= "2006-01-01"]
R = IN.pct_change(fill_method=None)
bad = (R < -0.35) | (R > 0.5)
# rebuild a price path with split-like jumps removed (treat those days as 0 % return)
Rc = R.mask(bad, 0.0)
Pc = (1 + Rc.fillna(0)).cumprod().where(IN.notna())
q.xs_tests(Pc, "Nifty 500 (split jumps removed)", q.C_INSTK); q.trend_ath(Pc, "Nifty 500 (split jumps removed)", q.C_INSTK)
pd.DataFrame(q.RES).to_csv("data_store/research/quantpedia_sweep_india_clean.csv", index=False)
