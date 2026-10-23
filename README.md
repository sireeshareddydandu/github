# Health & Safety Forms Studio

## Portals
- User submission portal: `http://127.0.0.1:5000/submit`
- Admin sign-in: `http://127.0.0.1:5000/admin/login`
- Admin workspace and submissions: `http://127.0.0.1:5000/admin`

## Features
- Mobile-responsive form and installable PWA manifest/service worker.
- Separate user submission and administrator portals.
- Admin portal protected by credentials in `.env`.
- SQLite database stored separately in `data/forms_app.sqlite3`.
- Timestamps stored in ISO-8601 UTC format (`Z`) and displayed to users in local time.
- Admin can view submissions and export all submissions as CSV.
- Each respondent can download only their own submission using a private receipt link.
- Existing SQLite databases are migrated without deleting saved responses.

## Run on Windows PowerShell
From the folder containing `app.py` and `requirements.txt`:
```powershell
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe app.py
```

## First admin login
On first run, `.env` is created with `ADMIN_USERNAME`, `ADMIN_PASSWORD`, and `SECRET_KEY`. Read the password from that file and keep it private.

## Mobile use
To use on a phone, deploy to a server reachable by the phone over HTTPS, then use the browser's **Add to Home Screen** / **Install app** option. This is a PWA-ready web app, not a published Play Store/App Store package.

## Security notes
- Do not expose Flask's development server directly to the internet. Deploy behind HTTPS using a production WSGI server and firewall.
- The receipt link is a bearer link; anyone with it can download that submission. Keep it private.
- Current admin authentication uses one admin credential from `.env`. Individually managed multiple-admin accounts and roles are not yet implemented.
- Back up the `data` folder regularly.
