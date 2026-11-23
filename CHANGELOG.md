# Changelog

Each release has a Docker image at `ghcr.io/eviled3/payday:<version>`. Database changes are applied when Payday starts.

Note changes under **Unreleased** as they're made. When releasing, rename that heading to the new version number.

## Unreleased

- **Sync now asks your bank for the latest** through Akahu's refresh, waiting up to a minute for the banks to answer.
  Scheduled syncs still use Akahu's own daily refresh.
- **Bank data age** on each account (Settings, Accounts) and in the Overview status line, with a heads-up when a bank
  hasn't sent new data for over 30 hours or its connection needs attention.

## 1.0.0

First public release.

- **Pay-cycle budget**: what you can spend each day until payday, from income, bills due and spending so far.
  Monthly, fortnightly, weekly or calendar-month pay.
- **Bank sync** through Akahu (personal app, or Akahu Connect for hosted sites), daily and on demand.
- **Bills** matched to their payments automatically, suggested from your transactions, weekend due dates moved to Monday.
- **Goals**: save towards a target by a date, from a linked savings account or amounts added by hand, with the amount
  needed each pay, week and day, an on-track check and a projected date. Optionally budgeted like a bill.
- **Summary**: income, spending, categories, payees, tags, bills, daily spending and a day-rate chart with a warning line.
- **Transactions**: search as you type, notes and tags on every line, choose and resize columns, rules that learn.
- **Overview** bill totals: paid, pending, to come and overdue.
- **Mortgage** tracker with history import, forecasts and what-ifs.
- **Households** with invites, read-only members, and one login across several households.
- **Security**: two-step sign-in for everyone, encrypted bank tokens, passphrase-locked backups, CSV export.
- Light and dark themes, five colour schemes, works on phones and installs as an app.
