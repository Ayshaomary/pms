"""Computes the project dashboard, mirroring the sections of the PES daily tracker sheet:
KEY FIGURES, LIVE PROGRESS, DEVELOPER WORKLOAD (SP by sprint), MILESTONE TRACKER,
plus sprint progress and a burndown derived from task completion times.
"""

from collections import defaultdict
from datetime import timedelta

from django.utils import timezone

from accounts.serializers import display_name

from .models import Issue, Membership, Risk, Task


def _pct(part, whole):
    return round(part * 100.0 / whole, 1) if whole else 0.0


def build_dashboard(project):
    today = timezone.localdate()
    tasks = list(project.tasks.select_related("assignee", "sprint", "milestone"))
    sprints = list(project.sprints.all())
    milestones = list(project.milestones.select_related("sprint"))
    memberships = list(project.memberships.select_related("user"))

    total_sp = sum(t.story_points for t in tasks)
    done_tasks = [t for t in tasks if t.status == Task.Status.DONE]
    sp_done = sum(t.story_points for t in done_tasks)
    earned_sp = sum(t.story_points * t.percent_done / 100.0 for t in tasks)
    status_counts = defaultdict(int)
    for t in tasks:
        status_counts[t.status] += 1

    # Developers = members with the developer role, plus anyone holding tasks.
    dev_users = {m.user_id: m.user for m in memberships if m.role == Membership.Role.DEVELOPER}
    for t in tasks:
        if t.assignee_id:
            dev_users.setdefault(t.assignee_id, t.assignee)

    has_s0 = any(s.name.strip().upper() == "S0" for s in sprints)
    sprint_label = f"{len(sprints)} (incl. S0)" if has_s0 else str(len(sprints))

    key_figures = {
        "total_sp": total_sp,
        "tasks": len(tasks),
        "developers": len(dev_users),
        "sprints": len(sprints),
        "sprints_label": sprint_label,
        "release_date": project.release_date,
    }

    live_progress = {
        "sp_done": sp_done,
        "percent_complete": _pct(sp_done, total_sp),
        "earned_sp": round(earned_sp, 1),
        "percent_earned": _pct(earned_sp, total_sp),
        "tasks_done": status_counts[Task.Status.DONE],
        "tasks_in_progress": status_counts[Task.Status.IN_PROGRESS],
        "tasks_blocked": status_counts[Task.Status.BLOCKED],
        "tasks_not_started": status_counts[Task.Status.NOT_STARTED],
        "open_risks": project.risks.filter(status__in=Risk.OPEN_STATUSES).count(),
        "open_issues": project.issues.filter(status__in=Issue.OPEN_STATUSES).count(),
        "overdue_tasks": sum(
            1 for t in tasks if t.due_date and t.due_date < today and t.status != Task.Status.DONE
        ),
    }

    # --- Developer workload: story points (not dates!) summed per developer per sprint.
    sp_by = defaultdict(lambda: defaultdict(int))  # user_id -> sprint_id|None -> sp
    done_by = defaultdict(int)
    for t in tasks:
        if t.assignee_id:
            sp_by[t.assignee_id][t.sprint_id] += t.story_points
            if t.status == Task.Status.DONE:
                done_by[t.assignee_id] += t.story_points
    unassigned_sp = sum(t.story_points for t in tasks if not t.assignee_id)
    has_unscheduled = any(t.sprint_id is None for t in tasks)

    rows = []
    for user_id, user in sorted(dev_users.items(), key=lambda kv: display_name(kv[1]).lower()):
        by_sprint = {str(s.id): sp_by[user_id][s.id] for s in sprints}
        row_total = sum(sp_by[user_id].values())
        rows.append({
            "user": {"id": user.id, "username": user.username, "name": display_name(user)},
            "by_sprint": by_sprint,
            "unscheduled": sp_by[user_id][None],
            "total": row_total,
            "done": done_by[user_id],
            "percent_of_total": _pct(row_total, total_sp),
        })
    column_totals = {str(s.id): sum(t.story_points for t in tasks if t.sprint_id == s.id and t.assignee_id) for s in sprints}
    workload = {
        "sprints": [{"id": s.id, "name": s.name} for s in sprints],
        "rows": rows,
        "totals": {
            "by_sprint": column_totals,
            "unscheduled": sum(t.story_points for t in tasks if t.sprint_id is None and t.assignee_id),
            "total": total_sp - unassigned_sp,
        },
        "has_unscheduled": has_unscheduled,
        "unassigned_sp": unassigned_sp,
    }

    # --- Milestones
    ms_items = []
    for m in milestones:
        m_tasks = [t for t in tasks if t.milestone_id == m.id]
        m_total = sum(t.story_points for t in m_tasks)
        m_done = sum(t.story_points for t in m_tasks if t.status == Task.Status.DONE)
        ms_items.append({
            "id": m.id,
            "name": m.name,
            "due_date": m.due_date,
            "sprint_name": m.sprint.name if m.sprint else None,
            "is_done": m.is_done,
            "overdue": bool(m.due_date and m.due_date < today and not m.is_done),
            "task_count": len(m_tasks),
            "sp_total": m_total,
            "sp_done": m_done,
            "progress": _pct(m_done, m_total),
        })
    open_ms = [m for m in ms_items if not m["is_done"]]
    # Earliest due first; undated milestones keep their manual order at the end.
    open_ms_sorted = sorted(open_ms, key=lambda m: (m["due_date"] is None, m["due_date"] or today))
    next_open = open_ms_sorted[0] if open_ms_sorted else None

    # --- Sprint progress
    sprint_items = []
    current_sprint_id = None
    for s in sprints:
        s_tasks = [t for t in tasks if t.sprint_id == s.id]
        s_total = sum(t.story_points for t in s_tasks)
        s_done = sum(t.story_points for t in s_tasks if t.status == Task.Status.DONE)
        is_current = bool(s.start_date and s.end_date and s.start_date <= today <= s.end_date)
        if is_current and current_sprint_id is None:
            current_sprint_id = s.id
        sprint_items.append({
            "id": s.id, "name": s.name, "start_date": s.start_date, "end_date": s.end_date,
            "task_count": len(s_tasks), "sp_total": s_total, "sp_done": s_done,
            "progress": _pct(s_done, s_total), "is_current": is_current,
        })

    # --- Window & burndown
    start = project.start_date or min((s.start_date for s in sprints if s.start_date), default=None)
    end = project.end_date or project.release_date or max((s.end_date for s in sprints if s.end_date), default=None)
    burndown = None
    window = None
    if start and end and end >= start:
        days = (end - start).days
        completed_on = defaultdict(int)
        for t in done_tasks:
            if t.completed_at:
                completed_on[timezone.localdate(t.completed_at)] += t.story_points
        # Tasks completed before the window opened count on day one.
        burned = sum(sp for d, sp in completed_on.items() if d < start)
        points = []
        for i in range(days + 1):
            d = start + timedelta(days=i)
            burned += completed_on.get(d, 0)
            ideal = round(total_sp - total_sp * i / days, 1) if days else 0
            points.append({
                "date": d,
                "ideal": ideal,
                "remaining": (total_sp - burned) if d <= today else None,
            })
        burndown = points
        window = {
            "start": start,
            "end": end,
            "days_total": days + 1,
            "days_elapsed": max(0, min(days + 1, (today - start).days + 1)),
            "days_left": max(0, (end - today).days),
        }

    return {
        "project": {
            "id": project.id, "name": project.name, "code": project.code,
            "description": project.description, "start_date": project.start_date,
            "end_date": project.end_date, "release_date": project.release_date,
        },
        "today": today,
        "window": window,
        "key_figures": key_figures,
        "live_progress": live_progress,
        "workload": workload,
        "milestones": {"next_open": next_open, "items": ms_items},
        "sprints": sprint_items,
        "current_sprint_id": current_sprint_id,
        "burndown": burndown,
    }
