# Changelog

Each release has a Docker image at `ghcr.io/eviled3/payday:<version>`. Database changes are applied when Payday starts.

Note changes under **Unreleased** as they're made. When releasing, rename that heading to the new version number.

## Unreleased

- **Security fixes** from a review:
  - Emailed links (password resets, email checks, approvals) no longer use the address the request was sent to,
    which a stranger could set to their own site. They use `PUBLIC_URL`, or a local address when it isn't set.
  - Sign-in limits can't be dodged any more: forwarding headers are only believed from proxies on this machine or
    your network (`TRUSTED_PROXIES`), and flooding the limiter with junk names no longer resets every counter.
  - A wrong password no longer holds a server thread for a second, so a burst of them can't slow Payday for everyone.
  - Notifications can only be sent to the browsers' own push services, and a device can't be moved to another login.
  - Regular-expression rules give up after a moment instead of hanging on patterns like `(a+)+$`, and are limited to
    200 characters.
  - Smaller limits on restore uploads (25 MB file, 100 MB unpacked), a 256-character limit on passwords, a warning
    for a short `PAYDAY_KEY`, and a safer redirect after the privacy toggle.
- **Tests**: a first set of automated tests (`pytest tests`), run with a dependency check on every push and pull
  request before the image is built. GitHub Actions are pinned to exact versions, with Dependabot keeping them,
  the Python packages and the base image up to date.
- **Refunds take spending back off**: money back from a shop you spend with goes to that shop's category instead
  of counting as income, so a return no longer raises your daily amount. It's marked "Looks like a refund" for review.
- **Bills no longer look like overspending**: a category's usual amount is now its day-to-day spending plus the
  bills due in it this pay, so a quarterly or yearly bill landing this pay doesn't turn the category red or send an
  over-limit alert.
- **Summary's "Kept" waits for bills to come**: while a pay is still running it reads "Kept so far" with the bills
  still to come taken off, and "Where your income went" shows them as their own slice.
- **Tests** for the daily-number maths: refunds, bills against limits.

- **Notifications** on your phone or computer (Settings, Notifications), chosen per person and per household:
  bills coming up or missed, pay landed, day rate under your warning line, category limits, big purchases, sync
  problems, stale bank data, the last pay's result, a weekly summary, goals reached and transactions to check.
  Quiet hours hold alerts until morning. Uses standard web push, so no extra app is needed; iPhone needs Payday
  added to the home screen, and notifications need your https address.

- **Sync now asks your bank for the latest** through Akahu's refresh, waiting up to a minute for the banks to answer.
  Scheduled syncs still use Akahu's own daily refresh.
- **Bank data age** on the Overview pay card (top right), on each account (Settings, Accounts) and in the status line, with a heads-up when a bank
  hasn't sent new data for over 30 hours or its connection needs attention.
- **Bank data time now updates after a sync.** The "Bank data from …" label on the Overview and in the footer showed
  the oldest account, so one slow or closed account kept it stuck. It now shows the newest data, and the footer names any
  account that is more than an hour behind. Accounts more than 30 hours behind are still flagged as before.

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
