"""Import tasks from a spreadsheet-exported CSV (e.g. the 'Task Tracker' sheet).

Header names are matched loosely (case/spacing-insensitive) and title rows above
the header are skipped. Rows with a code that already exists update that task.
"""

import csv
import io
import re
from datetime import datetime

from django.db import transaction

from .models import Membership, Milestone, Sprint, Task

ALIASES = {
    "code": ["code", "id", "taskid", "ref", "req", "reqid", "requirement", "fr", "ticket", "key"],
    "title": ["task", "title", "taskname", "name", "summary", "deliverable", "activity"],
    "description": ["description", "details", "notes"],
    "sprint": ["sprint", "iteration"],
    "assignee": ["owner", "assignee", "developer", "assignedto", "dev", "responsible"],
    "story_points": ["sp", "storypoints", "points", "storypoint", "estimate"],
    "status": ["status", "state"],
    "percent_done": ["%done", "percentdone", "done%", "progress", "%complete", "complete%", "pctdone"],
    "priority": ["priority"],
    "start_date": ["start", "startdate", "begin"],
    "due_date": ["due", "duedate", "end", "enddate", "finish", "deadline"],
    "milestone": ["milestone"],
}

STATUS_MAP = {
    "done": Task.Status.DONE, "complete": Task.Status.DONE, "completed": Task.Status.DONE, "closed": Task.Status.DONE,
    "inprogress": Task.Status.IN_PROGRESS, "wip": Task.Status.IN_PROGRESS, "ongoing": Task.Status.IN_PROGRESS,
    "started": Task.Status.IN_PROGRESS, "review": Task.Status.IN_PROGRESS, "testing": Task.Status.IN_PROGRESS,
    "blocked": Task.Status.BLOCKED, "onhold": Task.Status.BLOCKED, "stuck": Task.Status.BLOCKED,
}

DATE_FORMATS = ["%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%d %b %Y", "%d %B %Y", "%d-%b-%Y", "%d-%b-%y", "%d/%m/%y", "%b %d, %Y"]


def _norm(s):
    return re.sub(r"[\s_\-.]", "", (s or "").strip().lower())


def _map_header(row):
    mapping = {}
    for idx, cell in enumerate(row):
        key = _norm(cell)
        for field, names in ALIASES.items():
            if key in names and field not in mapping:
                mapping[field] = idx
                break
    return mapping


def _parse_date(value):
    value = (value or "").strip()
    if not value:
        return None
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            continue
    return None


def _parse_int(value, default=0):
    try:
        return int(round(float(str(value).replace("%", "").replace(",", "").strip())))
    except (TypeError, ValueError):
        return default


def _parse_percent(value):
    raw = (value or "").strip()
    if not raw:
        return 0
    try:
        num = float(raw.replace("%", "").strip())
    except ValueError:
        return 0
    if "%" not in raw and 0 < num <= 1 and "." in raw:
        num *= 100  # 0.5 -> 50%
    return max(0, min(100, int(round(num))))


def import_tasks(project, text, user):
    rows = list(csv.reader(io.StringIO(text)))
    header_idx, mapping = None, {}
    for i, row in enumerate(rows[:30]):
        m = _map_header(row)
        if "title" in m:
            header_idx, mapping = i, m
            break
    if header_idx is None:
        return {"created": 0, "updated": 0, "skipped": 0,
                "warnings": ["No header row with a 'Task' or 'Title' column was found in the first 30 rows."]}

    members = list(Membership.objects.filter(project=project).select_related("user"))
    lookup = {}
    for m in members:
        u = m.user
        for k in [u.username, u.email, u.get_full_name(), u.first_name]:
            if k:
                lookup.setdefault(k.strip().lower(), u)

    sprints = {s.name.strip().lower(): s for s in project.sprints.all()}
    milestones = {ms.name.strip().lower(): ms for ms in project.milestones.all()}
    existing = {t.code.strip().lower(): t for t in project.tasks.exclude(code="")}

    def cell(row, field):
        idx = mapping.get(field)
        return row[idx].strip() if idx is not None and idx < len(row) else ""

    created = updated = skipped = 0
    warnings = []
    with transaction.atomic():
        for line_no, row in enumerate(rows[header_idx + 1:], start=header_idx + 2):
            title = cell(row, "title")
            if not title:
                skipped += 1
                continue

            sprint = None
            sprint_name = cell(row, "sprint")
            if sprint_name:
                sprint = sprints.get(sprint_name.lower())
                if sprint is None:
                    digits = re.findall(r"\d+", sprint_name)
                    sprint = Sprint.objects.create(project=project, name=sprint_name,
                                                   order=int(digits[0]) if digits else len(sprints))
                    sprints[sprint_name.lower()] = sprint

            milestone = None
            ms_name = cell(row, "milestone")
            if ms_name:
                milestone = milestones.get(ms_name.lower())
                if milestone is None:
                    milestone = Milestone.objects.create(project=project, name=ms_name, order=len(milestones))
                    milestones[ms_name.lower()] = milestone

            assignee = None
            who = cell(row, "assignee")
            if who:
                assignee = lookup.get(who.lower())
                if assignee is None:
                    warnings.append(f"Row {line_no}: '{who}' is not a project member — left unassigned.")

            status_raw = _norm(cell(row, "status"))
            status = STATUS_MAP.get(status_raw, Task.Status.NOT_STARTED)
            priority = _norm(cell(row, "priority"))

            values = {
                "title": title[:255],
                "description": cell(row, "description"),
                "sprint": sprint,
                "milestone": milestone,
                "assignee": assignee,
                "story_points": max(0, _parse_int(cell(row, "story_points"))),
                "status": status,
                "percent_done": _parse_percent(cell(row, "percent_done")),
                "priority": priority if priority in Task.Priority.values else Task.Priority.MEDIUM,
                "start_date": _parse_date(cell(row, "start_date")),
                "due_date": _parse_date(cell(row, "due_date")),
            }
            if values["percent_done"] == 100 and status == Task.Status.NOT_STARTED:
                values["status"] = Task.Status.DONE

            code = cell(row, "code")[:30]
            task = existing.get(code.lower()) if code else None
            if task:
                for k, v in values.items():
                    if k == "description" and not v:
                        continue
                    setattr(task, k, v)
                task.save()
                updated += 1
            else:
                task = Task.objects.create(project=project, code=code, created_by=user, **values)
                if code:
                    existing[code.lower()] = task
                created += 1

    return {"created": created, "updated": updated, "skipped": skipped, "warnings": warnings[:50],
            "columns": sorted(mapping.keys())}
