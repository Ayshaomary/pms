from django.conf import settings
from django.db import models
from django.db.models import Q


class Notification(models.Model):
    class Kind(models.TextChoices):
        ASSIGNED = "assigned", "Task assigned to you"
        MENTIONED = "mentioned", "Mentioned in a comment"
        COMMENTED = "commented", "Comment on your task"
        BLOCKED = "blocked", "Task blocked"
        OVERDUE = "overdue", "Task overdue"
        MILESTONE_DONE = "milestone_done", "Milestone completed"
        ADDED = "added", "Added to a project"
        INVITED = "invited", "Invited to a project"
        INVITE_ACCEPTED = "invite_accepted", "Invitation accepted"
        INVITE_DECLINED = "invite_declined", "Invitation declined"

    recipient = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="notifications")
    project = models.ForeignKey("projects.Project", on_delete=models.CASCADE, related_name="notifications")
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    kind = models.CharField(max_length=20, choices=Kind.choices)
    text = models.CharField(max_length=300)
    task = models.ForeignKey("projects.Task", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    milestone = models.ForeignKey("projects.Milestone", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    # Set for alerts that must only ever be created once (e.g. "overdue" per task + due date).
    dedupe_key = models.CharField(max_length=100, blank=True)
    is_read = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        indexes = [models.Index(fields=["recipient", "is_read", "-created_at"])]
        constraints = [
            models.UniqueConstraint(
                fields=["recipient", "dedupe_key"], condition=~Q(dedupe_key=""), name="unique_notification_dedupe",
            ),
        ]

    def __str__(self):
        return f"{self.recipient} · {self.kind} · {self.text[:40]}"
