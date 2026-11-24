# Payday on TrueNAS

These steps are for TrueNAS SCALE 24.10 or later, where apps run on Docker. Payday runs as a **custom app** from the
published image, `ghcr.io/eviled3/payday`.

## Install

1. **Datasets**: create a dataset for Payday's data, for example `tank/apps/payday`. It holds the database and
   `payday.key`, so include it in your snapshots and backups. Payday hands the folder to its own user (uid 10001)
   when it starts, so the default permissions are fine.
2. **Apps → Discover Apps → ⋮ → Install via YAML**. Name it `payday` and paste this, changing the dataset path,
   the version and the settings:

   ```yaml
   services:
     payday:
       image: ghcr.io/eviled3/payday:1.2.0   # the version to run; see Releases
       restart: unless-stopped
       ports:
         - "8000:8000"
       environment:
         TZ_NAME: Pacific/Auckland
         PAYDAY_DAY: "15"
         SYNC_HOUR: "6"
         # The https address you reach Payday on from outside, if any (Cloudflare tunnel, reverse proxy).
         # Emailed links (password resets, email checks) use it.
         # PUBLIC_URL: https://payday.example.co.nz
         # Only if your proxy isn't on this machine or your local network:
         # TRUSTED_PROXIES: 203.0.113.10/32
       volumes:
         - /mnt/tank/apps/payday:/data
   ```

3. Open `http://<truenas-address>:8000`, create your login and connect your bank (see the [README](../README.md)).

## Update to a new version

Each release is listed under [Releases](https://github.com/EvilEd3/Payday/releases) with what changed and any
settings to check.

1. Take a snapshot of the Payday dataset (Datasets → the dataset → Create Snapshot). Database changes are applied
   when the new version starts, so the snapshot is your way back.
2. **Apps → Installed → payday → Edit**, change the version in `image:` (for example `1.1.0` to `1.2.0`), add any new
   settings the release notes mention, and **Save**. TrueNAS pulls the new image and restarts Payday.
3. Check the version at the bottom of any page in Payday.

To go back, set the old version in `image:` again and roll the dataset back to the snapshot.

If you'd rather follow every release automatically, use `ghcr.io/eviled3/payday:latest`. TrueNAS doesn't pull a tag
it already has by itself, so after a release use **Edit → Save** on the app (or the app's pull image option, where
your TrueNAS version has one) to fetch the new `latest`.
