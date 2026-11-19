"""Automatic alerts. Called from the project API views at the moment something happens.

Rules
- Nobody is notified about their own action.
- Only current project members receive alerts for that project.
- "Overdue" alerts are created once per task + due date (dedupe_key), so re-checking is safe.
"""

import re

from django.db.models import Exists, OuterRef, Q
from django.utils import timezone

from accounts.serializers import display_name
from projects.models import Membership, Task

from .models import Notification

MENTION_RE = re.compile(r"(?<![\w@])@([A-Za-z0-9_.+-]+)")
Kind = Notification.Kind


def _task_label(task):
    return f"{task.code} {task.title}".strip() if task.code else task.title


def _clip(text, n=120):
    text = " ".join(text.split())
    return text if len(text) <= n else text[: n - 1] + "…"


def _notify(recipients, project, actor, kind, text, task=None, milestone=None, dedupe_key=""):
    member_ids = set(Membership.objects.filter(project=project).values_list("user_id", flat=True))
    actor_id = getattr(actor, "id", None)
    seen, rows = set(), []
    for user in recipients:
        if user is None or user.id == actor_id or user.id in seen or user.id not in member_ids:
            continue
        seen.add(user.id)
        rows.append(Notification(
            recipient=user, project=project, actor=actor if actor_id else None, kind=kind, text=text[:300],
            task=task, milestone=milestone, dedupe_key=dedupe_key,
        ))
    # ignore_conflicts: a deduplicated alert that already exists is silently skipped.
    Notification.objects.bulk_create(rows, ignore_conflicts=bool(dedupe_key))
    return len(rows)


def _managers(project):
    return [m.user for m in Membership.objects.filter(
        project=project, role__in=[Membership.Role.OWNER, Membership.Role.MANAGER]).select_related("user")]


def on_task_created(task, actor):
    if task.assignee_id:
        _notify([task.assignee], task.project, actor, Kind.ASSIGNED,
                f"{display_name(actor)} assigned you {_task_label(task)}", task=task)
    if task.status == Task.Status.BLOCKED:
        _blocked(task, actor)


def on_task_updated(task, before, actor):
    """`before` holds the task's assignee_id and status prior to the update."""
    if task.assignee_id and task.assignee_id != before.get("assignee_id"):
        _notify([task.assignee], task.project, actor, Kind.ASSIGNED,
                f"{display_name(actor)} assigned you {_task_label(task)}", task=task)
    if task.status == Task.Status.BLOCKED and before.get("status") != Task.Status.BLOCKED:
        _blocked(task, actor)


def _blocked(task, actor):
    recipients = _managers(task.project)
    if task.assignee:
        recipients.append(task.assignee)
    _notify(recipients, task.project, actor, Kind.BLOCKED,
            f"{_task_label(task)} was marked Blocked by {display_name(actor)}", task=task)


def on_comment(comment, actor):
    task = comment.task
    project = task.project
    snippet = _clip(comment.body)

    usernames = {m.lower().rstrip(".") for m in MENTION_RE.findall(comment.body)}
    mentioned = []
    if usernames:
        mentioned = [m.user for m in Membership.objects.filter(project=project).select_related("user")
                     if m.user.username.lower() in usernames]
        _notify(mentioned, project, actor, Kind.MENTIONED,
                f"{display_name(actor)} mentioned you on {_task_label(task)}: “{snippet}”", task=task)

    mentioned_ids = {u.id for u in mentioned}
    if task.assignee and task.assignee_id not in mentioned_ids:
        _notify([task.assignee], project, actor, Kind.COMMENTED,
                f"{display_name(actor)} commented on your task {_task_label(task)}: “{snippet}”", task=task)


def on_milestone_done(milestone, actor):
    members = [m.user for m in Membership.objects.filter(project=milestone.project).select_related("user")]
    _notify(members, milestone.project, actor, Kind.MILESTONE_DONE,
            f"Milestone reached: {milestone.name}", milestone=milestone)


def on_invited(invitation, actor):
    # The invitee isn't a member yet, so this bypasses _notify's member filter.
    Notification.objects.create(
        recipient=invitation.user, project=invitation.project, actor=actor, kind=Kind.INVITED,
        text=f"{display_name(actor)} invited you to {invitation.project.name} as {invitation.get_role_display()}"[:300],
    )


def clear_invite_alerts(invitation):
    """Remove the invitee's 'invited' alert once the invitation is answered or withdrawn."""
    Notification.objects.filter(recipient=invitation.user, project=invitation.project, kind=Kind.INVITED).delete()


def on_invite_answered(invitation, accepted):
    project, invitee = invitation.project, invitation.user
    if accepted:
        kind, verb = Kind.INVITE_ACCEPTED, "accepted"
    else:
        kind, verb = Kind.INVITE_DECLINED, "declined"
    _notify([invitation.invited_by], project, invitee, kind,
            f"{display_name(invitee)} {verb} your invitation to {project.name}")


def check_overdue(user=None, today=None):
    """Create one 'overdue' alert per overdue task for its owner. Safe to run repeatedly."""
    today = today or timezone.localdate()
    tasks = (
        Task.objects.filter(due_date__lt=today, assignee__isnull=False)
        .exclude(status=Task.Status.DONE)
        .select_related("project", "assignee")
    )
    if user is not None:
        tasks = tasks.filter(assignee=user)
    created = 0
    for task in tasks:
        days = (today - task.due_date).days
        created += _notify(
            [task.assignee], task.project, None, Kind.OVERDUE,
            f"{_task_label(task)} is overdue: it was due {task.due_date:%d %b} ({days} day{'s' if days != 1 else ''} ago)",
            task=task, dedupe_key=f"overdue:{task.id}:{task.due_date.isoformat()}",
        )
    return created


def visible_to(user):
    """A user's notifications, limited to projects they still belong to (plus pending invitations)."""
    is_member = Exists(Membership.objects.filter(project=OuterRef("project"), user=user))
    return Notification.objects.filter(
        Q(recipient=user) & (Q(is_member) | Q(kind=Kind.INVITED))
    ).select_related("actor", "project", "task", "milestone")
