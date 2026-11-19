"""Excel workbook for a whole project: download it, fill it in, import it back.

The same workbook serves three purposes:
- a blank template, imported to create a new project;
- an export of an existing project, filled with its current data;
- an update: edit that export and import it again — any time, as often as needed.

Every sheet except Project has an ID column. Rows with an ID update that record; rows without one
are new. Tasks without an ID are also matched by Code, sprints and milestones by Name. Rows deleted
from the sheet are left alone in the project (the import never deletes anything).
"""

import io
import re
from dataclasses import dataclass
from datetime import date, datetime

from django.db import transaction
from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.datavalidation import DataValidation

from notifications import service as alerts

from . import activity
from .models import Issue, Membership, Milestone, Project, Risk, Sprint, Task

MAX_ROWS = 5000
MAX_BYTES = 5 * 1024 * 1024
VALIDATION_ROWS = 500  # how far down the dropdowns reach

YES_NO = ["Yes", "No"]


@dataclass
class Col:
    header: str
    attr: str
    width: int = 14
    kind: str = "text"        # text | int | date | bool | choice | ref
    choices: list = None      # labels shown in the dropdown (kind == choice)
    source: str = None        # dropdown range on another sheet (kind == ref)
    required: bool = False
    note: str = ""


def _labels(choices):
    return [label for _, label in choices]


SHEETS = {
    "Sprints": [
        Col("ID", "id", 8, "int"),
        Col("Name", "name", 14, required=True, note="e.g. S0, S1 …"),
        Col("Order", "order", 8, "int"),
        Col("Start", "start_date", 13, "date"),
        Col("End", "end_date", 13, "date"),
        Col("Goal", "goal", 40),
    ],
    "Milestones": [
        Col("ID", "id", 8, "int"),
        Col("Name", "name", 30, required=True),
        Col("Due date", "due_date", 13, "date"),
        Col("Sprint", "sprint", 12, "ref", source="Sprints!$B$2:$B$%d" % VALIDATION_ROWS),
        Col("Done", "is_done", 8, "bool", choices=YES_NO),
        Col("Description", "description", 40),
    ],
    "Tasks": [
        Col("ID", "id", 8, "int"),
        Col("Code", "code", 10, note="e.g. FR-2.1"),
        Col("Title", "title", 36, required=True),
        Col("Description", "description", 36),
        Col("Sprint", "sprint", 10, "ref", source="Sprints!$B$2:$B$%d" % VALIDATION_ROWS),
        Col("Milestone", "milestone", 20, "ref", source="Milestones!$B$2:$B$%d" % VALIDATION_ROWS),
        Col("Owner", "assignee", 14, "ref", source="Team!$A$2:$A$%d" % VALIDATION_ROWS, note="username"),
        Col("SP", "story_points", 6, "int"),
        Col("Status", "status", 13, "choice", choices=_labels(Task.Status.choices)),
        Col("% Done", "percent_done", 8, "int"),
        Col("Priority", "priority", 10, "choice", choices=_labels(Task.Priority.choices)),
        Col("Start", "start_date", 13, "date"),
        Col("Due", "due_date", 13, "date"),
    ],
    "Risks": [
        Col("ID", "id", 8, "int"),
        Col("Title", "title", 36, required=True),
        Col("Description", "description", 36),
        Col("Probability", "probability", 12, "choice", choices=_labels(Risk.Level.choices)),
        Col("Impact", "impact", 10, "choice", choices=_labels(Risk.Level.choices)),
        Col("Status", "status", 12, "choice", choices=_labels(Risk.Status.choices)),
        Col("Owner", "owner", 14, "ref", source="Team!$A$2:$A$%d" % VALIDATION_ROWS, note="username"),
        Col("Mitigation", "mitigation", 36),
    ],
    "Issues": [
        Col("ID", "id", 8, "int"),
        Col("Title", "title", 36, required=True),
        Col("Description", "description", 36),
        Col("Severity", "severity", 10, "choice", choices=_labels(Issue.Severity.choices)),
        Col("Status", "status", 12, "choice", choices=_labels(Issue.Status.choices)),
        Col("Owner", "owner", 14, "ref", source="Team!$A$2:$A$%d" % VALIDATION_ROWS, note="username"),
        Col("Task", "task", 12, note="task Code"),
    ],
}

PROJECT_FIELDS = [
    ("Name", "name", "Required"),
    ("Code", "code", "Short key, e.g. PES"),
    ("Description", "description", ""),
    ("Window start", "start_date", "Today or later for a new project; not after window end or release"),
    ("Window end", "end_date", "Future date; not after the release date"),
    ("Release date", "release_date", "Future date for a new project"),
]

HEAD_FONT = Font(bold=True, color="FFFFFF")
HEAD_FILL = PatternFill("solid", fgColor="2F5597")
ID_FILL = PatternFill("solid", fgColor="7F7F7F")
NOTE_FONT = Font(italic=True, color="7F7F7F")


class WorkbookError(Exception):
    def __init__(self, errors):
        super().__init__("; ".join(errors))
        self.errors = errors


# ---------------------------------------------------------------- export

def _display(obj, col):
    value = getattr(obj, col.attr)
    if col.kind == "bool":
        return "Yes" if value else "No"
    if col.kind == "choice":
        return getattr(obj, f"get_{col.attr}_display")()
    if col.attr in ("sprint", "milestone"):
        return value.name if value else None
    if col.attr in ("assignee", "owner"):
        return value.username if value else None
    if col.attr == "task":
        return (value.code or str(value.id)) if value else None
    return value if value != "" else None


def _header(ws, headers):
    ws.append(headers)
    for i, h in enumerate(headers, start=1):
        c = ws.cell(row=1, column=i)
        c.font = HEAD_FONT
        c.fill = ID_FILL if h == "ID" else HEAD_FILL
        c.alignment = Alignment(vertical="center")
    ws.freeze_panes = "A2"


def build_workbook(project=None):
    """Return .xlsx bytes. With no project, a blank template."""
    wb = Workbook()

    info = wb.active
    info.title = "Instructions"
    info.column_dimensions["A"].width = 110
    lines = [
        "Project workbook",
        "",
        "1. Fill in the Project sheet, then add rows to Sprints, Milestones, Tasks, Risks and Issues.",
        "2. Only the grey ID column should be left untouched: blank ID = new row, existing ID = update that row.",
        "3. Dropdowns list the allowed values. Owner is a username from the Team sheet.",
        "4. Dates: use real Excel dates or YYYY-MM-DD.",
        "5. Import the file from the app: Projects → Import from Excel (new project), or the project's",
        "   Team → Excel workbook → Import (update). You can re-import an edited copy as often as you like.",
        "6. Removing a row from the sheet does not delete it from the project — delete it in the app instead.",
        "7. Team members can't be added here: invite them from the Team page.",
    ]
    for line in lines:
        info.append([line])
    info["A1"].font = Font(bold=True, size=14)

    ws = wb.create_sheet("Project")
    _header(ws, ["Field", "Value", "Notes"])
    for label, attr, note in PROJECT_FIELDS:
        ws.append([label, getattr(project, attr) or None if project else None, note])
        ws.cell(row=ws.max_row, column=1).font = Font(bold=True)
        ws.cell(row=ws.max_row, column=3).font = NOTE_FONT
    ws.column_dimensions["A"].width = 16
    ws.column_dimensions["B"].width = 50
    ws.column_dimensions["C"].width = 60

    querysets = {}
    if project is not None:
        querysets = {
            "Sprints": project.sprints.all(),
            "Milestones": project.milestones.select_related("sprint"),
            "Tasks": project.tasks.select_related("sprint", "milestone", "assignee").order_by(
                "sprint__order", "sprint__start_date", "id"),
            "Risks": project.risks.select_related("owner").order_by("id"),
            "Issues": project.issues.select_related("owner", "task").order_by("id"),
        }

    for name, cols in SHEETS.items():
        ws = wb.create_sheet(name)
        _header(ws, [c.header for c in cols])
        for i, col in enumerate(cols, start=1):
            letter = get_column_letter(i)
            ws.column_dimensions[letter].width = col.width
            rng = f"{letter}2:{letter}{VALIDATION_ROWS}"
            if col.choices:
                dv = DataValidation(type="list", formula1='"%s"' % ",".join(col.choices), allow_blank=True)
            elif col.source:
                dv = DataValidation(type="list", formula1=f"={col.source}", allow_blank=True)
                dv.showErrorMessage = False  # a new sprint/milestone name typed here is fine
            else:
                continue
            ws.add_data_validation(dv)
            dv.add(rng)
        for obj in querysets.get(name, []):
            ws.append([_display(obj, c) for c in cols])
            for i, col in enumerate(cols, start=1):
                if col.kind == "date":
                    ws.cell(row=ws.max_row, column=i).number_format = "yyyy-mm-dd"

    team = wb.create_sheet("Team")
    _header(team, ["Username", "Name", "Email", "Role"])
    for w, letter in zip([16, 24, 30, 16], "ABCD"):
        team.column_dimensions[letter].width = w
    if project is not None:
        for m in Membership.objects.filter(project=project).select_related("user"):
            team.append([m.user.username, m.user.get_full_name() or m.user.username, m.user.email,
                         m.get_role_display()])
    team.append([])
    team.append(["Reference only — invite people from the Team page in the app."])
    team.cell(row=team.max_row, column=1).font = NOTE_FONT

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


# ---------------------------------------------------------------- import

def _norm(s):
    return re.sub(r"[\s_\-.%]", "", str(s or "").strip().lower())


def _text(v):
    if v is None:
        return ""
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    return str(v).strip()


class _Reader:
    """Parses cell values, collecting warnings (fixable) and errors (block the import)."""

    def __init__(self):
        self.errors, self.warnings = [], []

    def where(self, sheet, row, col):
        return f"{sheet} row {row}, {col.header}"

    def date(self, v, at):
        if v in (None, ""):
            return None
        if isinstance(v, datetime):
            return v.date()
        if isinstance(v, date):
            return v
        s = _text(v)
        for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%d %b %Y", "%d %B %Y", "%d-%b-%Y", "%m/%d/%Y"):
            try:
                return datetime.strptime(s, fmt).date()
            except ValueError:
                continue
        self.errors.append(f"{at}: '{s}' is not a date (use YYYY-MM-DD).")
        return None

    def int(self, v, at, default=0, lo=0, hi=None):
        if v in (None, ""):
            return default
        try:
            n = int(round(float(_text(v).replace("%", "").replace(",", ""))))
        except ValueError:
            self.warnings.append(f"{at}: '{_text(v)}' is not a number — used {default}.")
            return default
        if isinstance(v, float) and 0 < v <= 1 and at.endswith("% Done"):
            n = int(round(v * 100))  # Excel percentage format stores 50% as 0.5
        if n < lo or (hi is not None and n > hi):
            clamped = max(lo, min(hi if hi is not None else n, n))
            self.warnings.append(f"{at}: {n} is out of range — used {clamped}.")
            n = clamped
        return n

    def choice(self, v, at, choices, default):
        """`choices` is a Django choices list of (value, label)."""
        if v in (None, ""):
            return default
        key = _norm(v)
        for value, label in choices:
            if key in (_norm(value), _norm(label)):
                return value
        allowed = ", ".join(label for _, label in choices)
        self.warnings.append(f"{at}: '{_text(v)}' isn't one of {allowed} — used the default.")
        return default

    def bool(self, v):
        return _norm(v) in ("yes", "y", "true", "1", "done", "x")


def _rows(wb, name, cols, reader):
    """Yield (row_number, {attr: raw value}) for non-empty rows of a sheet."""
    if name not in wb.sheetnames:
        return
    ws = wb[name]
    it = ws.iter_rows(values_only=True)
    header = next(it, None)
    if header is None:
        return
    by_header = {_norm(c.header): c for c in cols}
    index = {}
    for i, h in enumerate(header):
        col = by_header.get(_norm(h))
        if col and col.attr not in index:
            index[col.attr] = i
    for n, row in enumerate(it, start=2):
        if n > MAX_ROWS + 1:
            reader.errors.append(f"{name}: more than {MAX_ROWS} rows.")
            return
        values = {attr: (row[i] if i < len(row) else None) for attr, i in index.items()}
        if all(v in (None, "") for v in values.values()):
            continue
        yield n, values


def _col(sheet, attr):
    return next(c for c in SHEETS[sheet] if c.attr == attr)


def _project_values(wb, reader):
    if "Project" not in wb.sheetnames:
        reader.errors.append("The workbook has no 'Project' sheet. Start from the downloaded template.")
        return {}
    by_label = {_norm(label): attr for label, attr, _ in PROJECT_FIELDS}
    out = {}
    for row in wb["Project"].iter_rows(min_row=2, values_only=True):
        if not row or row[0] is None:
            continue
        attr = by_label.get(_norm(row[0]))
        if attr:
            out[attr] = row[1] if len(row) > 1 else None
    data = {}
    for _, attr, _ in PROJECT_FIELDS:
        v = out.get(attr)
        if attr.endswith("_date"):
            d = reader.date(v, f"Project, {attr.replace('_', ' ')}")
            data[attr] = d.isoformat() if d else None
        else:
            data[attr] = _text(v)
    return data


def import_workbook(upload, user, project=None):
    """Create a project (project=None) or update one from an uploaded .xlsx file.

    All or nothing: on any error nothing is saved and WorkbookError lists the problems.
    """
    from .serializers import ProjectSerializer  # avoid a circular import

    if upload.size > MAX_BYTES:
        raise WorkbookError([f"The file is larger than {MAX_BYTES // (1024 * 1024)} MB."])
    try:
        wb = load_workbook(upload, read_only=True, data_only=True)
    except Exception:
        raise WorkbookError(["This isn't a readable Excel (.xlsx) file."])

    reader = _Reader()
    creating = project is None
    summary = {k.lower(): {"created": 0, "updated": 0} for k in SHEETS}

    data = _project_values(wb, reader)
    if reader.errors:
        raise WorkbookError(reader.errors)
    if creating and not data.get("name"):
        raise WorkbookError(["Project sheet: Name is required."])
    if not creating and not data.get("name"):
        data.pop("name")  # keep the current name rather than blanking it
    ser = ProjectSerializer(project, data=data, partial=not creating)
    if not ser.is_valid():
        for field, msgs in ser.errors.items():
            reader.errors.append(f"Project sheet, {field.replace('_', ' ')}: {' '.join(str(m) for m in msgs)}")
        raise WorkbookError(reader.errors)

    with transaction.atomic():
        if creating:
            project = ser.save(created_by=user)
            Membership.objects.create(project=project, user=user, role=Membership.Role.OWNER)
            activity.log(project, user, "created", project)
        else:
            fields = [attr for _, attr, _ in PROJECT_FIELDS]
            before = activity.snapshot(project, fields)
            ser.save()
            changes = activity.diff(before, activity.snapshot(project, fields))
            if changes:
                activity.log(project, user, "updated", project, changes=changes)

        users = {}
        for m in Membership.objects.filter(project=project).select_related("user"):
            u = m.user
            for k in (u.username, u.email, u.get_full_name()):
                if k:
                    users.setdefault(k.strip().lower(), u)

        def person(v, at):
            name = _text(v)
            if not name:
                return None
            u = users.get(name.lower())
            if u is None:
                reader.warnings.append(f"{at}: '{name}' is not a project member — left empty.")
            return u

        def existing(model, ident, at):
            """The project's record with this ID (ignored when creating, so exports can be reused as copies)."""
            if creating or ident in (None, ""):
                return None
            pk = reader.int(ident, at, default=None)
            obj = model.objects.filter(project=project, pk=pk).first() if pk else None
            if obj is None:
                reader.warnings.append(f"{at}: no record with ID {_text(ident)} in this project — added as new.")
            return obj

        def upsert(sheet, obj, model, values, n):
            key = sheet.lower()
            if obj is None:
                obj = model.objects.create(project=project, **values)
                summary[key]["created"] += 1
                return obj, True
            for k, v in values.items():
                setattr(obj, k, v)
            obj.save()
            summary[key]["updated"] += 1
            return obj, False

        # Sprints
        sprints = {s.name.strip().lower(): s for s in project.sprints.all()}
        for n, row in _rows(wb, "Sprints", SHEETS["Sprints"], reader):
            w = lambda attr: reader.where("Sprints", n, _col("Sprints", attr))  # noqa: E731
            name = _text(row.get("name"))[:50]
            if not name:
                reader.warnings.append(f"Sprints row {n}: no Name — skipped.")
                continue
            start, end = reader.date(row.get("start_date"), w("start_date")), reader.date(row.get("end_date"), w("end_date"))
            if start and end and end < start:
                reader.errors.append(f"Sprints row {n}: End must be after Start.")
            obj = existing(Sprint, row.get("id"), w("id")) or sprints.get(name.lower())
            values = {"name": name, "start_date": start, "end_date": end, "goal": _text(row.get("goal")),
                      "order": reader.int(row.get("order"), w("order"),
                                          default=obj.order if obj else len(sprints))}
            obj, _ = upsert("Sprints", obj, Sprint, values, n)
            sprints[name.lower()] = obj

        def sprint_ref(v):
            name = _text(v)[:50]
            if not name:
                return None
            if name.lower() not in sprints:
                digits = re.findall(r"\d+", name)
                sprints[name.lower()] = Sprint.objects.create(
                    project=project, name=name, order=int(digits[0]) if digits else len(sprints))
                summary["sprints"]["created"] += 1
            return sprints[name.lower()]

        # Milestones
        milestones = {m.name.strip().lower(): m for m in project.milestones.all()}
        done_before = {m.id for m in milestones.values() if m.is_done}
        for n, row in _rows(wb, "Milestones", SHEETS["Milestones"], reader):
            w = lambda attr: reader.where("Milestones", n, _col("Milestones", attr))  # noqa: E731
            name = _text(row.get("name"))[:200]
            if not name:
                reader.warnings.append(f"Milestones row {n}: no Name — skipped.")
                continue
            obj = existing(Milestone, row.get("id"), w("id")) or milestones.get(name.lower())
            values = {"name": name, "due_date": reader.date(row.get("due_date"), w("due_date")),
                      "sprint": sprint_ref(row.get("sprint")), "is_done": reader.bool(row.get("is_done")),
                      "description": _text(row.get("description"))}
            if obj is None:
                values["order"] = len(milestones)
            obj, _ = upsert("Milestones", obj, Milestone, values, n)
            milestones[name.lower()] = obj

        def milestone_ref(v):
            name = _text(v)[:200]
            if not name:
                return None
            if name.lower() not in milestones:
                milestones[name.lower()] = Milestone.objects.create(project=project, name=name, order=len(milestones))
                summary["milestones"]["created"] += 1
            return milestones[name.lower()]

        # Tasks
        tasks_by_code = {t.code.strip().lower(): t for t in project.tasks.exclude(code="")}
        task_alerts = []
        for n, row in _rows(wb, "Tasks", SHEETS["Tasks"], reader):
            w = lambda attr: reader.where("Tasks", n, _col("Tasks", attr))  # noqa: E731
            title = _text(row.get("title"))[:255]
            if not title:
                reader.warnings.append(f"Tasks row {n}: no Title — skipped.")
                continue
            code = _text(row.get("code"))[:30]
            obj = existing(Task, row.get("id"), w("id")) or (tasks_by_code.get(code.lower()) if code else None)
            status = reader.choice(row.get("status"), w("status"), Task.Status.choices, Task.Status.NOT_STARTED)
            pct = reader.int(row.get("percent_done"), w("percent_done"), lo=0, hi=100)
            if pct == 100 and status == Task.Status.NOT_STARTED and row.get("status") in (None, ""):
                status = Task.Status.DONE
            values = {
                "code": code, "title": title, "description": _text(row.get("description")),
                "sprint": sprint_ref(row.get("sprint")), "milestone": milestone_ref(row.get("milestone")),
                "assignee": person(row.get("assignee"), w("assignee")),
                "story_points": reader.int(row.get("story_points"), w("story_points")),
                "status": status, "percent_done": pct,
                "priority": reader.choice(row.get("priority"), w("priority"), Task.Priority.choices,
                                          Task.Priority.MEDIUM),
                "start_date": reader.date(row.get("start_date"), w("start_date")),
                "due_date": reader.date(row.get("due_date"), w("due_date")),
            }
            before = {"assignee_id": obj.assignee_id, "status": obj.status} if obj else None
            if obj is None:
                values["created_by"] = user
            obj, created = upsert("Tasks", obj, Task, values, n)
            task_alerts.append((obj, before))
            if code:
                tasks_by_code[code.lower()] = obj

        # Risks
        for n, row in _rows(wb, "Risks", SHEETS["Risks"], reader):
            w = lambda attr: reader.where("Risks", n, _col("Risks", attr))  # noqa: E731
            title = _text(row.get("title"))[:255]
            if not title:
                reader.warnings.append(f"Risks row {n}: no Title — skipped.")
                continue
            values = {
                "title": title, "description": _text(row.get("description")),
                "probability": reader.choice(row.get("probability"), w("probability"), Risk.Level.choices,
                                             Risk.Level.MEDIUM),
                "impact": reader.choice(row.get("impact"), w("impact"), Risk.Level.choices, Risk.Level.MEDIUM),
                "status": reader.choice(row.get("status"), w("status"), Risk.Status.choices, Risk.Status.OPEN),
                "owner": person(row.get("owner"), w("owner")), "mitigation": _text(row.get("mitigation")),
            }
            upsert("Risks", existing(Risk, row.get("id"), w("id")), Risk, values, n)

        # Issues
        for n, row in _rows(wb, "Issues", SHEETS["Issues"], reader):
            w = lambda attr: reader.where("Issues", n, _col("Issues", attr))  # noqa: E731
            title = _text(row.get("title"))[:255]
            if not title:
                reader.warnings.append(f"Issues row {n}: no Title — skipped.")
                continue
            task = None
            task_ref = _text(row.get("task"))
            if task_ref:
                task = tasks_by_code.get(task_ref.lower())
                if task is None and task_ref.isdigit() and not creating:
                    task = Task.objects.filter(project=project, pk=int(task_ref)).first()
                if task is None:
                    reader.warnings.append(f"{w('task')}: no task with code '{task_ref}' — left empty.")
            obj = existing(Issue, row.get("id"), w("id"))
            values = {
                "title": title, "description": _text(row.get("description")),
                "severity": reader.choice(row.get("severity"), w("severity"), Issue.Severity.choices,
                                          Issue.Severity.MEDIUM),
                "status": reader.choice(row.get("status"), w("status"), Issue.Status.choices, Issue.Status.OPEN),
                "owner": person(row.get("owner"), w("owner")), "task": task,
            }
            if obj is None:
                values["reported_by"] = user
            upsert("Issues", obj, Issue, values, n)

        if reader.errors:
            raise WorkbookError(reader.errors[:50])  # leaving the atomic block rolls everything back

        total = sum(v["created"] + v["updated"] for v in summary.values())
        if total:
            activity.log(project, user, "imported", entity_type="workbook",
                         label=", ".join(f"{k}: {v['created']} new, {v['updated']} updated"
                                         for k, v in summary.items() if v["created"] or v["updated"]))

        def send_alerts():
            for task, before in task_alerts:
                if before is None:
                    alerts.on_task_created(task, user)
                else:
                    alerts.on_task_updated(task, before, user)
            for m in project.milestones.filter(is_done=True).exclude(id__in=done_before):
                alerts.on_milestone_done(m, user)

        transaction.on_commit(send_alerts)

    return project, {"summary": summary, "warnings": reader.warnings[:100], "created_project": creating}
