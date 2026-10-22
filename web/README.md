# adbust web

The website version: plug a phone into a **Windows 10/11 or Linux** PC, open
the site in **Chrome or Edge**, and clean it. **Mac is not supported**, and the
page says so in red.

1. **Connect phone.** You only press it the first time. After that, plugging the phone in is enough.
2. Every app is read with its real name and split into **user apps** and
   **system apps**.
3. Any user app that someone else already removed is listed in a prompt with
   *Remove ticked* / *Keep all*.
4. **Apps that opened** fills in live. Ads (AD) and apps drawing over the
   screen are ticked automatically. *Uninstall selected* removes everything
   ticked in one go. System apps can't be ticked.
5. Every app removed with the uninstall button — ticked by hand, caught
   popping up, or from the prompt — goes into the shared database for the
   next person. Ones that aren't adware are marked good in the hidden
   sorter (Ctrl+Shift+E), which takes them off the list.

## How it works

```
browser ──WebUSB──▶ phone          (adb protocol, in the page: Tango / ya-webadb)
browser ──HTTPS───▶ server.py      (the page, and the shared bad-app list)
```

The server never touches a phone. It serves `static/` and two endpoints:

| endpoint | |
|---|---|
| `GET /api/badapps` (or `/badapps.json`) | The shared list, in the same format as the repo's `badapps.json`, so the desktop app can sync from it: `ADBUST_DB_URL=https://abd.nexlink.com.au/badapps.json` |
| `POST /api/removed` | `{"client": "<random id>", "apps": [{"pkg", "label", "how"}]}` |

Guard rails on the shared list:
- Each browser counts **once per app**, so `reports` is the number of
  different people who removed it.
- Core system packages (`android`, `com.android.*`, `com.samsung.android.*`,
  …) are always refused.
- Each browser can report 200 apps a day, and each IP 600.
- Every removal is reported, with `how` = `popup`, `manual` or `database`.
  Apps marked good in the sorter (`POST /api/good`) are left out of
  `/api/badapps` whatever their reports say.

App names come from `helper/`, a ~4 KB dex run with `app_process`. It is
copied to `/data/local/tmp`, run once, and deleted. Nothing is installed on
the phone. Plain adb can't read app labels.

WebUSB only works on HTTPS (or `localhost`), so a LAN `http://` address
won't connect to phones. The site has to be reached through its HTTPS name.

## Run locally

```
python3 web/server.py          # http://localhost:8017
```

On Linux, run `adb kill-server` first: only one program can hold the phone's
USB connection.

## Rebuild the browser bundle or helper

Both outputs are committed, so the server needs only Python.

```
cd web/client && npm ci && npm run build   # -> static/app.js
web/helper/build.sh                        # -> static/adbust-applist.dex (needs Android SDK + JDK)
```

## Deploy (Willard)

A TrueNAS custom app, `adbust-web`, built on the host like the other
801X sites. Data lives in `/mnt/Pool1-MAIN/apps/adbust-web/data` (owned by
568, the apps user). Steps are in `deploy-willard.sh`. The public name
`abd.nexlink.com.au` is a Cloudflare Tunnel public hostname pointing at
`http://192.168.0.10:<port>`, and is added in the Cloudflare dashboard.
