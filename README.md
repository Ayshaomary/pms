# PMS: collaborative project tracker

A multi-user project management system modelled on the **PES PM Daily Tracker** spreadsheet.
Each user logs in, belongs to one or more projects with a role, and updates tasks (Status, % Done).
The dashboard recalculates the tracker's sections live.

- **backend/**: Django 5.2 + Django REST Framework (session auth with CSRF)
- **frontend/**: Angular 20 (standalone components, signals)

## Features

| Spreadsheet section | In the app |
|---|---|
| Key figures (Total SP, Tasks, Developers, Sprints, Release date) | Dashboard → Key figures |
| Live progress (SP done, % complete, done / in progress / blocked, open risks & issues) | Dashboard → Live progress, plus earned SP from partial % Done and overdue tasks |
| Developer workload (SP by sprint, total, % of total) | Dashboard → Developer workload, with the current sprint highlighted |
| Milestone tracker (next open milestone) | Dashboard → Milestone tracker, with SP progress for each milestone |
| Task Tracker sheet | **Task tracker**: filters, inline Status / % Done / owner edits, CSV import & export |
Aisha
The app also has a burndown chart, sprint progress, risk register (probability × impact), issue log,
comments on tasks, an activity feed of every change, and team management.

**Project status report (owners only):** go to **Report**, or **Print report** on the dashboard.
The owner chooses which sections to include and can add summary notes, then prints or saves the
report as a PDF (pick "Save as PDF" in the print dialog). The report includes a header, key
figures, items needing attention, burndown, milestones, sprints, workload, open risks and issues,
an optional full task list, the team, and sign-off lines.
The API (`/api/projects/{id}/report/`) refuses anyone who isn't an owner, and each report generated
is recorded in the activity feed.

### Automatic alerts

A bell in the top bar collects alerts. It refreshes every 30 seconds and shows a toast when something new arrives.
Clicking an alert opens the task (or milestone) it refers to. You're never alerted about your own actions, and
only current project members receive a project's alerts.

| When | Who is alerted |
|---|---|
| A task is assigned (or reassigned) to someone | the new owner |
| Someone is @mentioned in a comment (type `@` for suggestions) | the mentioned members |
| Someone comments on a task | the task's owner |
| A task becomes **Blocked** | its owner, and the project's managers and owners |
| A task is past its due date and not done | its owner, once per task and due date |
| A milestone is marked done | the whole team |
| Someone is added to a project | that person |

Overdue alerts are generated whenever someone opens their notifications, so nothing needs scheduling.
To have them waiting even for people who haven't opened the app, schedule
`python manage.py send_overdue_alerts` daily (cron, for example `0 7 * * *`). It's safe to run repeatedly.

### Roles

| Role | Can |
|---|---|
| Viewer | see everything, comment on tasks |
| Developer | + create / update tasks, risks and issues |
| Project manager | + sprints, milestones, CSV import, members, project settings, deletes |
| Owner | + manage owners, print the project report, delete the project |

Whoever creates a project becomes its owner. Teammates register on their own; a manager then
adds them on **Team & settings** by username or email.

## Run locally

Requirements: Python 3.12+, Node 20+.

```bash
# Backend: http://localhost:8000
cd backend
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python manage.py migrate
.venv/bin/python manage.py seed_pes          # optional demo project
.venv/bin/python manage.py createsuperuser   # optional, for /admin
.venv/bin/python manage.py runserver

# Frontend: http://localhost:4200 (open this one)
cd frontend
npm install
npx ng serve
```

`ng serve` proxies `/api` and `/admin` to Django (`frontend/proxy.conf.json`), so the browser
sees a single origin and the session cookie works without CORS.

**Demo data:** `seed_pes` creates the PES project (16 Nov – 30 Dec 2026, sprints S0–S6, 51 tasks,
278 SP, 12 risks, 6 issues). Users: `pm` (owner) and `haroon`, `godfrey`, `elia`, `daniel`
(developers). All use the password `pes-demo-2026`; change it with `--password`. Task, risk and
issue titles are placeholders. Run `seed_pes --reset` to recreate the project.

## Importing your real Task Tracker

Export the *Task Tracker* sheet as CSV, then go to **Task tracker → Import CSV** (project managers
only). The importer:

- recognises these columns in any order: ID/Code, Task/Title, Sprint, Owner, SP, Status, % Done,
  Priority, Start, Due, Milestone, Description;
- skips title rows above the header;
- updates a task when its code already exists, so you can re-import safely;
- matches Owner to project members by username, email or full name. **Add the team before
  importing.** Unknown owners are left unassigned and listed as warnings;
- creates sprints and milestones it doesn't recognise;
- reads dates as `2026-11-16`, `16/11/2026` or `16 Nov 2026`.

## Tests

```bash
cd backend && .venv/bin/python manage.py test
cd frontend && npx ng build
```

## Production

1. Set the environment variables from `backend/.env.example`. Django reads them from the process
   environment, for example from a systemd `EnvironmentFile=`. For PostgreSQL, install
   `psycopg[binary]`.
2. Run the backend:
   ```bash
   pip install gunicorn
   python manage.py migrate
   python manage.py collectstatic
   gunicorn config.wsgi -b 127.0.0.1:8000
   ```
3. `cd frontend && npx ng build`, then serve `dist/frontend/browser` with nginx and proxy `/api`
   and `/admin` to gunicorn (example: `deploy/nginx.conf`). Keep the frontend and API on the same
   domain.

## API overview

All endpoints are under `/api/`.

- Auth: `auth/csrf/`, `auth/register/`, `auth/login/`, `auth/logout/`, `auth/me/`, `auth/password/`
- `notifications/`, `notifications/unread-count/`, `notifications/{id}/read/`, `notifications/read-all/`
- `projects/`, `projects/{id}/dashboard/`, `projects/{id}/activity/`, `projects/{id}/report/` (owners)
- `projects/{id}/` + `members/`, `sprints/`, `milestones/`, `tasks/` (with `export/` and
  `import/`), `comments/?task=`, `risks/`, `issues/`
# pms
