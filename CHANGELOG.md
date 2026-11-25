# Changelog

Each release has a Docker image at `ghcr.io/eviled3/payday:<version>`. Database changes are applied when Payday starts.

Note changes under **Unreleased** as they're made. When releasing, rename that heading to the new version number.

## Unreleased

- **Overview without gaps**: the cards under the pay card now share out between the two columns so both end level,
  instead of a short column leaving a big empty space beside a tall Accounts card. Phones keep a single column.
- **Settings, rearranged**: a list of sections down the side (grouped into Household, Spending, Records and You)
  replaces the strip of tabs across the top, which couldn't be scrolled with a mouse. On a phone, Settings opens as a
  list; tap a section to open it and "Settings" at the top to go back.
- **Activity moved into Settings**: the activity log is now under Settings, Records, to free up the top menu. Links
  from notifications open it just as before.
- Fixed the activity log's list showing unstyled on wider screens.

## 1.4.0

- **Sync details on the Overview**: hover over or tap "Bank data from ..." at the top of the Overview to see when
  Payday last synced, how fresh each account's bank data is, and how many new transactions came in. Tap anywhere
  else to close it.
- **Bills calendar** (Calendar in the menu, or More on a phone): every bill laid out by day, a pay period or a
  calendar month at a time. Go back through past periods to see what was paid and what never showed up, or forward
  to see what's coming. Totals for each day, each week, and the whole period (paid, to come, missed), plus a table of
  each bill. On a phone it lists just the days with bills.
- **Overview, simpler**: the strip of days under the pay card is gone. The Bills card now lists the bills still to
  come, nearest first, with how many days until each, and links to the calendar for the ones already paid.
- **Activity log**: a new Activity page (in the menu, and More on a phone) keeps a full record of your household:
  every transaction the bank sends and where it was allocated (a category, a bill, a transfer), each transfer Payday
  matched, transactions re-sorted after a rule or bill changed, each sync step by step, every notification sent and
  who got it, and the changes people make by hand. Filter by kind or search it. Admins choose what's recorded and how
  long it's kept, or clear it, in Settings, Activity log, and can change a transaction's category or bill straight
  from its entry.
- **Notifications open the right page**: tapping a notification opens its entry in the log, with the full message
  and a button to the bill, account or transactions it's about, in the right household. On Android, tapping one
  while Payday is in the background now brings it to the front instead of leaving you on the home screen.

## 1.3.0

- **Loans**: the Mortgage page is now Loans, and holds as many loans as you have: a mortgage, a car loan, a
  personal loan. Open one from its loan account (Settings, Accounts, or the account's page) or from More, Loans.
  Each loan shows what's owed, when it'll be paid off, interest paid and still to come, the rate history, and
  what-ifs for paying extra each repayment or a lump sum, less any early repayment fee. Repayments can be weekly,
  fortnightly or monthly. Your mortgage carries over as it was, and old Mortgage links still work.
- **A car's resale value in net worth**: a car loan counts the car as something you own. Payday starts from what you
  paid (the loan plus deposit) and takes 15% a year off, or whatever you set; add your own estimate any time. The loan
  page shows what it's worth against what you owe, and when the car becomes worth more than the loan.
- **Loan repayments set aside**: a repayment to a linked loan account counts as a transfer, so it wasn't held back
  from your daily amount. The loan page now finds a bill you already have for it and links it, or adds one from the
  last repayment in one click.

## 1.2.0

- **What's new**: click the version number at the bottom of any page to see what changed in each version.
  After an update, a "What's new" tag sits beside it until you've had a look.
- **Know before an account runs short**: the Overview follows each account through the bills still due before payday
  and warns when one won't cover them, naming the bills and the day. Each account shows where it'll be after its
  bills. A bill comes out of the account its last payment did, or one you pick (Bills, edit a bill, Comes out of).
  There's a matching notification, on by default (Settings, Notifications).
- **A heads-up when your bank can't cover the daily amount**: if the accounts you count hold noticeably less than
  this pay's budget expects (often money moved to savings), the Overview says so and links to the bank check.
- **Trends**: your last 6 or 12 pays side by side (Summary, Trends over time). Spending each pay with bills and day to
  day, what you kept and what share of income that was, averages over the finished pays, and your biggest
  categories pay by pay with the unusually high ones in bold.
- **Split a transaction across categories**: from a transaction's ··· menu, give parts of it their own categories
  and notes, such as groceries and cleaning in one supermarket shop. The Overview, Summary, Trends and limits count each
  part in its own category, and a category's transaction list shows its part. Bills and transfers aren't split.
- **Net worth** (More, Net worth; or from the Accounts card): everything you have less everything you owe, including
  KiwiSaver and savings, plus your home and mortgage from the Mortgage page. A month-by-month line for the last 12
  or 24 months, worked back from your transactions. Each sync now saves the day's balances, so values that change
  without transactions (like KiwiSaver) are tracked from now on.
- **Payee names keep their initials**: "ANZ Home Loan" and "AA Insurance" instead of "Anz" and "Aa".
- **First steps on a new household**: until your bank's accounts arrive, the Overview shows a short setup list
  instead of "$0.00 a day".

## 1.1.0

**Before updating:** if you reach Payday from outside your network (a Cloudflare tunnel or reverse proxy), set
`PUBLIC_URL` to that https address, or password-reset and email-check links won't be sent. If that proxy isn't on the
same machine or your local network, also set `TRUSTED_PROXIES` to its address.

- **TrueNAS guide** ([docs/truenas.md](docs/truenas.md)): installing Payday as a custom app on TrueNAS SCALE, and
  updating or rolling back.
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
