# Market calendar

Apple Calendar (iCalendar) subscriptions for market events, rebuilt four times a day by GitHub Actions.

| Calendar | What is in it | Subscribe |
|---|---|---|
| Earnings | Earnings reports of companies worth over $50B, with EPS forecast and before open / after close timing | `webcal://ritzywes.github.io/market-calendar/earnings.ics` |
| IPOs | Upcoming and priced IPOs raising over $100M | `webcal://ritzywes.github.io/market-calendar/ipos.ics` |
| Fed, ECB & US data | Fed and ECB rate decisions with press conferences, FOMC minutes, US GDP, PCE, CPI, jobs report, PPI, retail sales, JOLTS, trade balance | `webcal://ritzywes.github.io/market-calendar/macro.ics` |
| Markets | All of the above in one calendar | `webcal://ritzywes.github.io/market-calendar/all.ics` |

## Sources

Nasdaq (earnings, IPOs), federalreserve.gov (FOMC), ecb.europa.eu (ECB), bea.gov (GDP, PCE, trade), FRED (CPI, jobs, PPI, retail sales, JOLTS; only the next date of each is published ahead).

If a source is down, the events it gave last time are kept (`data/cache.json`), so the calendar never empties.

## Changing what shows up

Edit `config.json`:

- `earnings.min_market_cap_usd`: market cap cutoff
- `earnings.always_include`: tickers that always show, e.g. `["ASML", "ADYEN"]` (US listed symbols)
- `ipos.min_offer_usd`: minimum IPO size
- `us_data_fred`: which US releases to include (FRED release id: title)

Pushing a change rebuilds the feeds straight away. To rebuild by hand: Actions tab, "Update calendar", Run workflow.

Run locally: `python3 build.py` (Python 3.9+, no packages needed).
