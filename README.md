# Payday

**Know what you can spend today, until payday.**

Payday is a self-hosted budgeting app for New Zealand bank accounts. It runs on your pay cycle (monthly, fortnightly,
weekly, or calendar month), syncs daily through [Akahu](https://www.akahu.nz), ticks bills off as they're paid,
and divides what's left by the days until you're paid again.

![Overview](docs/screenshots/overview.png)

- **Pay-cycle budget**: income, every bill due before payday, goals you're budgeting for, and spending so far, as a
  daily amount. Monthly, fortnightly, weekly or calendar-month pay.
- **Bills** that match their payments automatically, including pending ones; suggested from your transactions;
  weekend due dates move to Monday.
- **Goals**: a target and a date, tracked from a linked savings account or amounts you add. Shows what's needed each
  pay, week and day, whether you're on track, and when you'll get there. Tick "Budget for this" to set the money aside
  from each pay like a bill.
- **Summary**: income, spending and what you kept; categories, payees and tags; bills paid and to come; daily spending
  with bills stacked; and a day-rate chart with a warning line that also flags the Overview.
- **Transactions**: search as you type, notes and tags on every line, choose your columns, rules that learn from you,
  and a review list for Payday's guesses.
- **Account reconciling** with running balances that match your banking app.
- **Loans**: mortgages, car and personal loans with weekly, fortnightly or monthly repayments. History import, payoff forecasts, extra-repayment and refix what-ifs, equity or a car's resale value, and milestones.
- **Households**: invite your partner (admin or read only); one login can belong to several households.
- **Secure by default**: two-step sign-in for everyone, encrypted bank tokens, backups you can lock with a passphrase.
- Light and dark themes in five colour schemes, a badge of your own, works on phones, installs as an app.

| | |
|---|---|
| ![Summary](docs/screenshots/summary.png) | ![Goals](docs/screenshots/goals.png) |
| ![Transactions](docs/screenshots/transactions.png) | ![Mortgage](docs/screenshots/mortgage.png) |

| | |
|---|---|
| ![Phone, light](docs/screenshots/phone.png) | ![Phone, dark](docs/screenshots/phone-dark.png) |

Screenshots show a made-up household.

## Install

You need Docker and an Akahu account.

Using the published image (recommended):

```sh
mkdir payday && cd payday
curl -O https://raw.githubusercontent.com/EvilEd3/Payday/main/docker-compose.release.yml
curl -o .env https://raw.githubusercontent.com/EvilEd3/Payday/main/.env.example   # set PUBLIC_URL if reached from outside
docker compose -f docker-compose.release.yml up -d
```

Or from source:

```sh
git clone https://github.com/EvilEd3/Payday.git payday && cd payday
cp .env.example .env
docker compose up -d --build
```

On TrueNAS SCALE, run it as a custom app: see [Payday on TrueNAS](docs/truenas.md) for installing and updating.

Open http://localhost:8000, create your login, scan the QR code with an authenticator app, and save the backup codes.

### Connect your bank (Akahu personal app)

1. Sign up at [my.akahu.nz](https://my.akahu.nz), confirm your identity and turn on two-step sign-in.
2. Connect your bank in Akahu and choose the accounts to share.
3. In my.akahu.nz open **Developers** and create a personal app.
4. Copy the **App ID Token** (`app_token_…`) and **User Access Token** (`user_token_…`).
5. In Payday: **Settings → Bank connection → Personal app tokens**, paste both, **Save and test**.
6. **Settings → Accounts**: tick what counts towards your budget. Then add your bills.

Akahu personal apps connect your own accounts only. Each household that self-hosts Payday uses its own personal app.

## Running it for other people

Payday can also run as a hosted service: public sign-up with email checks and approval, an Akahu **Connect your
bank** flow (needs an approved Akahu Full App), policy pages filled in from your business details, and PostgreSQL with
one schema per household so several copies of the app can run side by side. If you do this you hold other people's
bank data: talk to Akahu, read up on the Privacy Act 2020 and the Customer and Product Data Act, and have a lawyer
check the policies first.

## Settings

| Setting | |
|---|---|
| `PUBLIC_URL` | The https address people use. Payday then only answers to it, localhost and your local network. Emailed links (password resets, email checks) use it; without it, they're only sent when Payday was reached by a local address. |
| `TRUSTED_PROXIES` | Addresses or networks of proxies whose `CF-Connecting-IP`, `X-Forwarded-For` and `X-Forwarded-Proto` headers are believed. Default: this machine and private networks (covers cloudflared or a reverse proxy in Docker or on your network). |
| `PUSH_HOSTS` | Extra push service hosts for notifications, comma separated. Google, Mozilla, Apple and Microsoft's are built in. |
| `DATABASE_URL` / `DATABASE_URL_FILE` | `sqlite:////data/budget.db` (default) or `postgresql://user:pass@host/db`. |
| `PAYDAY_KEY` / `PAYDAY_KEY_FILE` | Encrypts bank tokens and 2FA keys. Created as `/data/payday.key` if not set; back it up with the database. |
| `AKAHU_CLIENT_ID`, `AKAHU_CLIENT_SECRET` | An approved Akahu Full App, for Connect your bank. Redirect: `PUBLIC_URL/bank/callback`. |
| `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`, `SMTP_FROM`, `SMTP_SECURITY` | Email for sign-up and password resets. Without it, emails go to the log. |
| `APP_USER`, `APP_PASSWORD` | Optional first login on a new install. |
| `PAYDAY_DAY`, `SYNC_HOUR`, `TZ_NAME` | Defaults for new households. |

## Backups and updates

- **Settings → Backup and export**: a backup of your household (optionally passphrase-locked) and CSV files.
- Also back up the `data` folder (the database and `payday.key`) while Payday is stopped.
- To update the published image: `docker compose -f docker-compose.release.yml pull && docker compose -f docker-compose.release.yml up -d`.
  From source: `git pull && docker compose up -d --build`. Database changes are applied on start.
- Releases and what changed: [Releases](https://github.com/EvilEd3/Payday/releases) and [CHANGELOG.md](CHANGELOG.md).
  `latest` is the newest release; pin a version (e.g. `ghcr.io/eviled3/payday:1.0.0`) to update only when you choose.
- Move from SQLite to PostgreSQL: `docker compose run --rm -e DATABASE_URL=postgresql://… payday python -m app.admin move-to-postgres /data`

## Security

Two-step sign-in for everyone; scrypt passwords; sign-in limits per device and per account; signed, HTTP-only,
SameSite cookies (Secure over HTTPS); no API; every request checks household membership and role; household data in
separate files or schemas; bank tokens and 2FA keys encrypted; strict content security policy; cross-site posts
refused; the container runs as an ordinary user. Put Payday behind HTTPS (a reverse proxy or Cloudflare Tunnel)
before exposing it to the internet. Found a problem? See [SECURITY.md](SECURITY.md).

## Licence

[GNU Affero General Public License v3.0](LICENSE). You can use, change and share Payday. If you run a changed version
for other people over a network, you must offer them its source code.
