# Where this folder's data lives now

On 2026-10-20 the market data from this folder was centralised in
**`Documents/MASTER_BACKTESTER/data/`** (see `MASTER_BACKTESTER/data/CENTRAL_DATA.md`).

Moved: `datasets/` (1.9 GB of recorded live sessions + Kite day sets) → `MASTER_BACKTESTER/data/external/nifty-live-tracker/datasets/`.

**Nothing broke:** each moved file/folder was replaced by a shortcut (symlink) pointing to the central copy,
so existing code that reads or writes the old path keeps working. New data written here lands in the central folder.

- Don't delete the shortcut(s) here — that breaks this project's paths.
- Don't copy data back into this folder; read it from the central path instead.
- Full move log: `MASTER_BACKTESTER/data/external/MOVES_2026-10-20.csv`
