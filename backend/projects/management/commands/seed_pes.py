"""Seed a demo of the PES project shaped like PES_PM_Daily_Tracker.csv.

Window 16 Nov – 30 Dec 2026, 7 weekly sprints (S0–S6), 4 developers, 51 tasks,
278 story points, 12 open risks, 6 open issues. Task/risk/issue titles are
placeholders — import the real Task Tracker with the "Import CSV" button.
"""

import random
from datetime import date, timedelta

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from projects.models import Issue, Membership, Milestone, Project, Risk, Sprint, Task

User = get_user_model()

DEVELOPERS = [
    ("haroon", "Haroon", "Ahmed"),
    ("godfrey", "Godfrey", "John"),
    ("elia", "Elia", "Willium"),
    ("daniel", "Daniel", "Tumaini"),
]

# Tasks per developer per sprint (S0..S6), as implied by the tracker's workload table.
TASKS_PER_SPRINT = {
    "haroon":  [0, 4, 3, 3, 2, 2, 1],
    "godfrey": [0, 2, 1, 3, 2, 2, 0],
    "elia":    [1, 1, 2, 2, 1, 1, 1],
    "daniel":  [0, 4, 3, 3, 2, 3, 2],
}

RISKS = [
    "Scope creep from late requirement changes", "Key developer unavailable during sprint",
    "Third-party API integration delays", "Test environment not ready in time",
    "Insufficient UAT participation", "Data migration quality issues",
    "Performance below target under load", "Security review findings late in cycle",
    "Holiday season reduces team capacity", "Unclear acceptance criteria",
    "Hosting / infrastructure provisioning delays", "Dependency on external approvals",
]

ISSUES = [
    ("Staging build failing intermittently", Issue.Severity.HIGH),
    ("Login redirect loop on mobile Safari", Issue.Severity.MEDIUM),
    ("Seed data missing for profile screens", Issue.Severity.LOW),
    ("CI pipeline exceeds 20 min", Issue.Severity.MEDIUM),
    ("Design specs for reports not final", Issue.Severity.HIGH),
    ("Email notifications landing in spam", Issue.Severity.MEDIUM),
]


class Command(BaseCommand):
    help = "Create the PES demo project, users, sprints, milestones, tasks, risks and issues."

    def add_arguments(self, parser):
        parser.add_argument("--password", default="pes-demo-2026", help="Password for all demo users")
        parser.add_argument("--reset", action="store_true", help="Delete an existing PES project first")

    @transaction.atomic
    def handle(self, *args, password, reset, **options):
        existing = Project.objects.filter(code="PES")
        if existing.exists():
            if not reset:
                raise CommandError("A project with code PES already exists. Use --reset to recreate it.")
            existing.delete()

        def user(username, first, last):
            u, created = User.objects.get_or_create(
                username=username,
                defaults={"first_name": first, "last_name": last, "email": f"{username}@example.com"},
            )
            if created:
                u.set_password(password)
                u.save()
            return u

        pm = user("pm", "Project", "Manager")
        devs = {username: user(username, first, last) for username, first, last in DEVELOPERS}

        start = date(2026, 11, 16)
        project = Project.objects.create(
            name="PES Project", code="PES", created_by=pm,
            description="Daily tracking for the PES delivery window.",
            start_date=start, end_date=date(2026, 12, 30), release_date=date(2026, 12, 30),
        )
        Membership.objects.create(project=project, user=pm, role=Membership.Role.OWNER)
        for u in devs.values():
            Membership.objects.create(project=project, user=u, role=Membership.Role.DEVELOPER)

        sprints = []
        for i in range(7):
            s_start = start + timedelta(weeks=i)
            s_end = min(s_start + timedelta(days=6), project.end_date)
            sprints.append(Sprint.objects.create(project=project, name=f"S{i}", order=i,
                                                 start_date=s_start, end_date=s_end))

        # 11×3 + 25×5 + 15×8 = 278 SP over 51 tasks.
        rng = random.Random(2026)
        points = [3] * 11 + [5] * 25 + [8] * 15
        rng.shuffle(points)

        tasks_by_code = {}
        counter = {i: 0 for i in range(7)}
        for username, per_sprint in TASKS_PER_SPRINT.items():
            for idx, count in enumerate(per_sprint):
                for _ in range(count):
                    counter[idx] += 1
                    code = f"FR-{idx}.{counter[idx]}"
                    sprint = sprints[idx]
                    tasks_by_code[code] = Task.objects.create(
                        project=project, code=code, title=f"Demo task {code}",
                        sprint=sprint, assignee=devs[username], story_points=points.pop(),
                        start_date=sprint.start_date, due_date=sprint.end_date, created_by=pm,
                    )

        milestones = [
            ("Sprint 0 setup complete", sprints[0], []),
            ("Profile core done (FR-2.1–2.4 demo)", sprints[2], ["FR-2.1", "FR-2.2", "FR-2.3", "FR-2.4"]),
            ("Feature complete", sprints[4], []),
            ("Release candidate signed off", sprints[5], []),
            ("Release", sprints[6], []),
        ]
        for order, (name, sprint, codes) in enumerate(milestones):
            ms = Milestone.objects.create(project=project, name=name, sprint=sprint,
                                          due_date=sprint.end_date, order=order)
            if not codes:
                codes = [c for c, t in tasks_by_code.items() if t.sprint_id == sprint.id]
            Task.objects.filter(project=project, code__in=codes).update(milestone=ms)

        people = [pm, *devs.values()]
        for i, title in enumerate(RISKS):
            Risk.objects.create(project=project, title=title, owner=people[i % len(people)],
                                probability=1 + i % 3, impact=1 + (i * 2) % 3)
        for i, (title, severity) in enumerate(ISSUES):
            Issue.objects.create(project=project, title=title, severity=severity,
                                 owner=people[(i + 1) % len(people)], reported_by=pm)

        total = sum(t.story_points for t in project.tasks.all())
        self.stdout.write(self.style.SUCCESS(
            f"Created '{project.name}' (id={project.id}): {project.tasks.count()} tasks, {total} SP, "
            f"{len(sprints)} sprints, {len(devs)} developers."
        ))
        self.stdout.write(f"Users: pm (owner), {', '.join(devs)} — password: {password}")
