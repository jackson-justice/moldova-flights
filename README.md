# moldova-flights

A personal flight price tracker for weekend trips out of Chisinau, Moldova (RMO) during a July–September stay.

## What it does

- Maintains a **rolling 6-week window** of upcoming Friday–Sunday weekends
- Fetches live flight prices from **Google Flights via SerpApi** for all destinations worldwide
- Checks 4 window types per weekend: Fri–Sun, Thu–Sun, Fri–Mon, Thu–Mon
- Logs all prices to a local **SQLite database** so booking timing patterns emerge over time
- Displays everything in an interactive **Streamlit dashboard** — sortable, filterable, color-coded by price
- Auto-refreshes every 2 days in the background — no manual intervention needed

## Budget targets

| Badge | Price (2 pax, roundtrip) |
|-------|--------------------------|
| Great deal | Under $400 |
| Good option | $400–$500 |
| OK | $500–$999 |
| Hidden | $1,000+ |

## Key constraints

- Departure airport: **RMO** (Chisinau) only
- Max 1 stop each way
- Hard cutoff: no data collected after **September 1st**
- Aug 18–22 permanently excluded (Hungary trip already booked)
- Wizz Air results flagged — actual price lower due to membership discount

## Tech stack

- Python 3.11+
- SerpApi (`google_travel_explore` engine)
- SQLite
- Streamlit
- pandas
- `schedule` for auto-refresh

## Setup

1. Clone the repo
```bash
git clone https://github.com/jackson-justice/moldova-flights.git
cd moldova-flights
```

2. Install dependencies
```bash
pip install -r requirements.txt
```

3. Create a `.env` file in the project root
```
SERPAPI_KEY=your_key_here
```

4. Run the dashboard
```bash
streamlit run src/dashboard.py
```

The database will be created automatically on first run at `data/flights.db`.

## Project structure

```
moldova-flights/
├── PLANNING.md          # Full project planning doc
├── requirements.txt
├── .env                 # API key — never committed
├── data/
│   └── flights.db       # SQLite database (auto-created)
├── src/
│   ├── config.py        # Constants and settings
│   ├── windows.py       # Rolling window logic
│   ├── fetcher.py       # SerpApi integration
│   ├── db.py            # Database helpers
│   ├── scheduler.py     # Auto-refresh every 2 days
│   └── dashboard.py     # Streamlit UI
└── tests/
    └── test_windows.py  # Unit tests for window logic
```

## API usage

Uses SerpApi free tier (250 searches/month). Thu–Mon window fetches weekly instead of every 2 days to stay within limits.
