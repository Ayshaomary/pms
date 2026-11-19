from django.contrib.auth import get_user_model
from django.db.models import Q
from django.utils import timezone
from rest_framework import serializers

from accounts.serializers import UserBriefSerializer

from .models import Activity, Comment, Invitation, Issue, Membership, Milestone, Project, Risk, Sprint, Task

User = get_user_model()


class ProjectSerializer(serializers.ModelSerializer):
    my_role = serializers.SerializerMethodField()
    member_count = serializers.IntegerField(read_only=True, default=0)
    task_count = serializers.IntegerField(read_only=True, default=0)
    sp_total = serializers.IntegerField(read_only=True, default=0)
    sp_done = serializers.IntegerField(read_only=True, default=0)

    class Meta:
        model = Project
        fields = [
            "id", "name", "code", "description", "start_date", "end_date", "release_date",
            "created_at", "updated_at", "my_role", "member_count", "task_count", "sp_total", "sp_done",
        ]
        read_only_fields = ["id", "created_at", "updated_at"]

    def get_my_role(self, obj):
        role = getattr(obj, "my_role", None)
        if role is None:
            request = self.context.get("request")
            m = obj.memberships.filter(user=request.user).first() if request else None
            role = m.role if m else None
        return role

    def validate(self, attrs):
        start = attrs.get("start_date", getattr(self.instance, "start_date", None))
        end = attrs.get("end_date", getattr(self.instance, "end_date", None))
        release = attrs.get("release_date", getattr(self.instance, "release_date", None))
        errors = {}

        # New projects must be planned forward from today. (Existing projects may already
        # have started, so editing them only checks the dates against each other.)
        if self.instance is None:
            today = timezone.localdate()
            if release and release <= today:
                errors["release_date"] = "Release date must be a future date."
            if start and start < today:
                errors["start_date"] = "Window start must be today or a future date."
            if end and end <= today:
                errors["end_date"] = "Window end must be a future date."

        if start and end and start > end and "start_date" not in errors:
            errors["start_date"] = "Window start can't be after the window end."
        if start and release and start > release and "start_date" not in errors:
            errors["start_date"] = "Window start can't be after the release date."
        if end and release and end > release and "end_date" not in errors:
            errors["end_date"] = "Window end can't be after the release date."

        if errors:
            raise serializers.ValidationError(errors)
        return attrs


class ProjectScopedSerializer(serializers.ModelSerializer):
    """Ensures related objects (sprint, assignee, …) belong to the current project."""

    def _project(self):
        return self.context["project"]

    def _check_member(self, user, field):
        if user and not Membership.objects.filter(project=self._project(), user=user).exists():
            raise serializers.ValidationError({field: "User is not a member of this project."})

    def _check_same_project(self, obj, field):
        if obj and obj.project_id != self._project().id:
            raise serializers.ValidationError({field: "Must belong to this project."})


class MembershipSerializer(serializers.ModelSerializer):
    """Existing members. New people join through an Invitation."""

    user = UserBriefSerializer(read_only=True)
    email = serializers.EmailField(source="user.email", read_only=True)

    class Meta:
        model = Membership
        fields = ["id", "user", "email", "role", "joined_at"]
        read_only_fields = ["id", "joined_at"]


class InvitationSerializer(serializers.ModelSerializer):
    user = UserBriefSerializer(read_only=True)
    email = serializers.EmailField(source="user.email", read_only=True)
    invited_by = UserBriefSerializer(read_only=True)
    identifier = serializers.CharField(write_only=True, help_text="Username or email of the user to invite")

    class Meta:
        model = Invitation
        fields = ["id", "user", "email", "role", "invited_by", "created_at", "identifier"]
        read_only_fields = ["id", "created_at"]

    def validate(self, attrs):
        identifier = (attrs.pop("identifier", "") or "").strip()
        if not identifier:
            raise serializers.ValidationError({"identifier": "Enter a username or email."})
        user = User.objects.filter(Q(username__iexact=identifier) | Q(email__iexact=identifier)).first()
        if user is None:
            raise serializers.ValidationError(
                {"identifier": "No user with that username or email. Ask them to register first."}
            )
        project = self.context["project"]
        if Membership.objects.filter(project=project, user=user).exists():
            raise serializers.ValidationError({"identifier": "That user is already a member."})
        if Invitation.objects.filter(project=project, user=user).exists():
            raise serializers.ValidationError({"identifier": "That user already has a pending invitation."})
        attrs["user"] = user
        return attrs


class MyInvitationSerializer(serializers.ModelSerializer):
    """An invitation as the invitee sees it."""

    invited_by = UserBriefSerializer(read_only=True)
    project_name = serializers.CharField(source="project.name", read_only=True)
    project_code = serializers.CharField(source="project.code", read_only=True)
    project_description = serializers.CharField(source="project.description", read_only=True)

    class Meta:
        model = Invitation
        fields = ["id", "project", "project_name", "project_code", "project_description", "role",
                  "invited_by", "created_at"]
        read_only_fields = fields


class SprintSerializer(ProjectScopedSerializer):
    sp_total = serializers.IntegerField(read_only=True, default=0)
    sp_done = serializers.IntegerField(read_only=True, default=0)
    task_count = serializers.IntegerField(read_only=True, default=0)

    class Meta:
        model = Sprint
        fields = ["id", "name", "order", "start_date", "end_date", "goal", "sp_total", "sp_done", "task_count"]

    def validate(self, attrs):
        start = attrs.get("start_date", getattr(self.instance, "start_date", None))
        end = attrs.get("end_date", getattr(self.instance, "end_date", None))
        if start and end and end < start:
            raise serializers.ValidationError({"end_date": "End date must be after the start date."})
        return attrs


class MilestoneSerializer(ProjectScopedSerializer):
    sp_total = serializers.IntegerField(read_only=True, default=0)
    sp_done = serializers.IntegerField(read_only=True, default=0)
    task_count = serializers.IntegerField(read_only=True, default=0)
    sprint_name = serializers.CharField(source="sprint.name", read_only=True, default=None)

    class Meta:
        model = Milestone
        fields = [
            "id", "name", "description", "due_date", "sprint", "sprint_name", "is_done", "completed_at",
            "order", "sp_total", "sp_done", "task_count",
        ]
        read_only_fields = ["completed_at"]

    def validate_sprint(self, value):
        self._check_same_project(value, "sprint")
        return value


class TaskSerializer(ProjectScopedSerializer):
    assignee_detail = UserBriefSerializer(source="assignee", read_only=True)
    sprint_name = serializers.CharField(source="sprint.name", read_only=True, default=None)
    milestone_name = serializers.CharField(source="milestone.name", read_only=True, default=None)
    comment_count = serializers.IntegerField(read_only=True, default=0)

    class Meta:
        model = Task
        fields = [
            "id", "code", "title", "description", "sprint", "sprint_name", "milestone", "milestone_name",
            "assignee", "assignee_detail", "story_points", "status", "percent_done", "priority",
            "start_date", "due_date", "completed_at", "created_at", "updated_at", "comment_count",
        ]
        read_only_fields = ["completed_at", "created_at", "updated_at"]

    def validate_sprint(self, value):
        self._check_same_project(value, "sprint")
        return value

    def validate_milestone(self, value):
        self._check_same_project(value, "milestone")
        return value

    def validate_assignee(self, value):
        self._check_member(value, "assignee")
        return value

    def validate(self, attrs):
        # Typing 100% without touching Status marks the task Done.
        if attrs.get("percent_done") == 100 and "status" not in attrs:
            attrs["status"] = Task.Status.DONE
        return attrs


class RiskSerializer(ProjectScopedSerializer):
    owner_detail = UserBriefSerializer(source="owner", read_only=True)
    score = serializers.IntegerField(read_only=True)

    class Meta:
        model = Risk
        fields = [
            "id", "title", "description", "probability", "impact", "score", "status", "owner",
            "owner_detail", "mitigation", "created_at", "updated_at",
        ]
        read_only_fields = ["created_at", "updated_at"]

    def validate_owner(self, value):
        self._check_member(value, "owner")
        return value


class IssueSerializer(ProjectScopedSerializer):
    owner_detail = UserBriefSerializer(source="owner", read_only=True)
    reported_by_detail = UserBriefSerializer(source="reported_by", read_only=True)
    task_label = serializers.SerializerMethodField()

    class Meta:
        model = Issue
        fields = [
            "id", "title", "description", "severity", "status", "owner", "owner_detail", "task",
            "task_label", "reported_by_detail", "resolved_at", "created_at", "updated_at",
        ]
        read_only_fields = ["resolved_at", "created_at", "updated_at"]

    def validate_owner(self, value):
        self._check_member(value, "owner")
        return value

    def get_task_label(self, obj):
        return str(obj.task) if obj.task_id else None

    def validate_task(self, value):
        self._check_same_project(value, "task")
        return value


class CommentSerializer(ProjectScopedSerializer):
    author_detail = UserBriefSerializer(source="author", read_only=True)

    class Meta:
        model = Comment
        fields = ["id", "task", "body", "author_detail", "created_at", "updated_at"]
        read_only_fields = ["created_at", "updated_at"]

    def validate_task(self, value):
        self._check_same_project(value, "task")
        if self.instance and value != self.instance.task:
            raise serializers.ValidationError("A comment can't be moved to another task.")
        return value

    def validate_body(self, value):
        if not value.strip():
            raise serializers.ValidationError("Comment can't be empty.")
        return value


class ActivitySerializer(serializers.ModelSerializer):
    actor = UserBriefSerializer(read_only=True)

    class Meta:
        model = Activity
        fields = ["id", "actor", "verb", "entity_type", "entity_id", "entity_label", "changes", "created_at"]
