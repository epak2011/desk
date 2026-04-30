# Trading Desk — local

Tactical decision interface. Any US ticker, real Yahoo Finance data, runs on your Mac. No deploy, no accounts, no rate limits.

---

## One-time setup (Mac)

You'll do this once. Takes ~3 minutes.

### 1. Install Python if you don't have it

Open **Terminal** (Cmd+Space, type "Terminal", hit Enter) and paste:

```
python3 --version
```

If you see a version number (3.9 or higher), skip to step 2.

If not, install Python from [python.org/downloads](https://www.python.org/downloads/) — download the macOS installer, run it, click through.

### 2. Install the app's dependencies

In Terminal, paste the following two lines (one at a time, hit Enter after each):

```
cd ~/Downloads/desk-local
pip3 install -r requirements.txt
```

*(If you put the folder somewhere other than Downloads, adjust the path.)*

This installs Streamlit, yfinance, pandas. Takes about 60 seconds.

### 3. Run the app

Still in Terminal:

```
streamlit run app.py
```

It opens a browser tab at `http://localhost:8501` automatically. That's the app.

---

## Daily use

Every time you want to use it after that first setup, just:

1. Open Terminal
2. Paste: `cd ~/Downloads/desk-local && streamlit run app.py`
3. Done. Browser tab opens with the app.

**Tip:** You can save that command as a Mac shortcut/alias if you want to launch it by clicking. Optional.

---

## What you can do

- **Type any US ticker** in the sidebar — NVDA, PLTR, HIMS, RKLB, anything. No hard-coded list.
- **Add to watchlist** to see the whole list grouped by Enter / Watch / Avoid
- **Log decisions** to the tracker; close them later and the tracker calculates your hit rate
- Watchlist, tracker, and saved API key live in `~/.desk_store.json` (your home folder, hidden file). This survives app upgrades — replacing the desk-local folder does not touch your data.
- **Paste an Anthropic API key** in the sidebar to get live Claude-generated Portfolio Manager views (thesis / drivers / risks / valuation) instead of static templates

---

## Troubleshooting

**"command not found: pip3"** → Your Python install didn't add pip to the path. Try `python3 -m pip install -r requirements.txt` instead.

**"Couldn't find data for X"** → Check the ticker symbol. Some foreign ADRs need a suffix (e.g. `BABA` works, `BABA.SW` doesn't).

**App looks cramped** → It's designed for wide screens. Drag the browser window wider.

**Streamlit keeps opening in Safari and you want Chrome** → Copy the `localhost:8501` URL from Safari into Chrome. First load takes a second; everything after is instant.

---

## Files

- `app.py` — Streamlit UI
- `tactical.py` — the tactical engine (bias, action, trigger, levels)
- `pm_view.py` — Portfolio Manager views, static + optional live Claude call
- `~/.desk_store.json` — your watchlist, tracker log, account size, and saved API key (created on first save, lives in your home folder so app upgrades preserve it)
- `requirements.txt` — Python dependencies

Edit `pm_view.py` to add hand-written theses for tickers you own. The app uses those as fallbacks when no Claude key is provided.
