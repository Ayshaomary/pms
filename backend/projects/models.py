from django.conf import settings
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.utils import timezone

User = settings.AUTH_USER_MODEL


class Project(models.Model):
    name = models.CharField(max_length=200)
    code = models.CharField(max_length=20, blank=True, help_text="Short key, e.g. PES")
    description = models.TextField(blank=True)
    start_date = models.DateField(null=True, blank=True)
    end_date = models.DateField(null=True, blank=True)
    release_date = models.DateField(null=True, blank=True)
    created_by = models.ForeignKey(User, null=True, on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return self.name


class Membership(models.Model):
    class Role(models.TextChoices):
        OWNER = "owner", "Owner"
        MANAGER = "manager", "Project manager"
        DEVELOPER = "developer", "Developer"
        VIEWER = "viewer", "Viewer"

    # Higher number = more rights. Used by permission checks.
    RANK = {Role.VIEWER: 0, Role.DEVELOPER: 1, Role.MANAGER: 2, Role.OWNER: 3}

    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="memberships")
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="memberships")
    role = models.CharField(max_length=20, choices=Role.choices, default=Role.DEVELOPER)
    joined_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = [("project", "user")]
        ordering = ["joined_at"]

    def __str__(self):
        return f"{self.user} @ {self.project} ({self.role})"

    @property
    def rank(self):
        return self.RANK[self.role]


class Invitation(models.Model):
    """A pending offer to join a project. Becomes a Membership when the invitee accepts."""

    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="invitations")
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="project_invitations")
    role = models.CharField(max_length=20, choices=Membership.Role.choices, default=Membership.Role.DEVELOPER)
    invited_by = models.ForeignKey(User, null=True, on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = [("project", "user")]
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.user} invited to {self.project} ({self.role})"


class Sprint(models.Model):
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="sprints")
    name = models.CharField(max_length=50, help_text="e.g. S0, S1 …")
    order = models.PositiveIntegerField(default=0)
    start_date = models.DateField(null=True, blank=True)
    end_date = models.DateField(null=True, blank=True)
    goal = models.TextField(blank=True)

    class Meta:
        ordering = ["order", "start_date", "id"]

    def __str__(self):
        return self.name


class Milestone(models.Model):
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="milestones")
    name = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    due_date = models.DateField(null=True, blank=True)
    sprint = models.ForeignKey(Sprint, null=True, blank=True, on_delete=models.SET_NULL, related_name="milestones")
    is_done = models.BooleanField(default=False)
    completed_at = models.DateTimeField(null=True, blank=True)
    order = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["order", "due_date", "id"]

    def __str__(self):
        return self.name

    def save(self, *args, **kwargs):
        if self.is_done and not self.completed_at:
            self.completed_at = timezone.now()
        elif not self.is_done:
            self.completed_at = None
        super().save(*args, **kwargs)


class Task(models.Model):
    class Status(models.TextChoices):
        NOT_STARTED = "not_started", "Not started"
        IN_PROGRESS = "in_progress", "In progress"
        BLOCKED = "blocked", "Blocked"
        DONE = "done", "Done"

    class Priority(models.TextChoices):
        LOW = "low", "Low"
        MEDIUM = "medium", "Medium"
        HIGH = "high", "High"

    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="tasks")
    code = models.CharField(max_length=30, blank=True, help_text="Requirement / ticket id, e.g. FR-2.1")
    title = models.CharField(max_length=255)
    description = models.TextField(blank=True)
    sprint = models.ForeignKey(Sprint, null=True, blank=True, on_delete=models.SET_NULL, related_name="tasks")
    milestone = models.ForeignKey(Milestone, null=True, blank=True, on_delete=models.SET_NULL, related_name="tasks")
    assignee = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name="assigned_tasks")
    story_points = models.PositiveIntegerField(default=0)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.NOT_STARTED)
    percent_done = models.PositiveSmallIntegerField(
        default=0, validators=[MinValueValidator(0), MaxValueValidator(100)]
    )
    priority = models.CharField(max_length=10, choices=Priority.choices, default=Priority.MEDIUM)
    start_date = models.DateField(null=True, blank=True)
    due_date = models.DateField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    created_by = models.ForeignKey(User, null=True, on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["sprint__order", "code", "id"]

    def __str__(self):
        return f"{self.code} {self.title}".strip()

    def save(self, *args, **kwargs):
        # Keep Status and % Done consistent, and stamp completion time for the burndown.
        if self.status == self.Status.DONE:
            self.percent_done = 100
            if not self.completed_at:
                self.completed_at = timezone.now()
        else:
            self.completed_at = None
            if self.percent_done >= 100:
                self.percent_done = 99
        super().save(*args, **kwargs)


class Risk(models.Model):
    class Level(models.IntegerChoices):
        LOW = 1, "Low"
        MEDIUM = 2, "Medium"
        HIGH = 3, "High"

    class Status(models.TextChoices):
        OPEN = "open", "Open"
        MONITORING = "monitoring", "Monitoring"
        CLOSED = "closed", "Closed"

    OPEN_STATUSES = [Status.OPEN, Status.MONITORING]

    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="risks")
    title = models.CharField(max_length=255)
    description = models.TextField(blank=True)
    probability = models.PositiveSmallIntegerField(choices=Level.choices, default=Level.MEDIUM)
    impact = models.PositiveSmallIntegerField(choices=Level.choices, default=Level.MEDIUM)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.OPEN)
    owner = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    mitigation = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["status", "-probability", "-impact", "id"]

    def __str__(self):
        return self.title

    @property
    def score(self):
        return self.probability * self.impact


class Issue(models.Model):
    class Severity(models.TextChoices):
        LOW = "low", "Low"
        MEDIUM = "medium", "Medium"
        HIGH = "high", "High"
        CRITICAL = "critical", "Critical"

    class Status(models.TextChoices):
        OPEN = "open", "Open"
        IN_PROGRESS = "in_progress", "In progress"
        RESOLVED = "resolved", "Resolved"
        CLOSED = "closed", "Closed"

    OPEN_STATUSES = [Status.OPEN, Status.IN_PROGRESS]

    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="issues")
    title = models.CharField(max_length=255)
    description = models.TextField(blank=True)
    severity = models.CharField(max_length=10, choices=Severity.choices, default=Severity.MEDIUM)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.OPEN)
    owner = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    task = models.ForeignKey(Task, null=True, blank=True, on_delete=models.SET_NULL, related_name="issues")
    reported_by = models.ForeignKey(User, null=True, on_delete=models.SET_NULL, related_name="+")
    resolved_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["status", "-created_at"]

    def __str__(self):
        return self.title

    def save(self, *args, **kwargs):
        if self.status in self.OPEN_STATUSES:
            self.resolved_at = None
        elif not self.resolved_at:
            self.resolved_at = timezone.now()
        super().save(*args, **kwargs)


class Comment(models.Model):
    task = models.ForeignKey(Task, on_delete=models.CASCADE, related_name="comments")
    author = models.ForeignKey(User, null=True, on_delete=models.SET_NULL, related_name="+")
    body = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["created_at"]


class Activity(models.Model):
    """Audit trail shown as the project's activity feed."""

    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="activities")
    actor = models.ForeignKey(User, null=True, on_delete=models.SET_NULL, related_name="+")
    verb = models.CharField(max_length=30)  # created / updated / deleted / commented …
    entity_type = models.CharField(max_length=30)
    entity_id = models.PositiveIntegerField(null=True, blank=True)
    entity_label = models.CharField(max_length=255, blank=True)
    changes = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        verbose_name_plural = "activities"
