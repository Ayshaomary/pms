import csv
import io

from django.db import transaction
from django.db.models import Count, IntegerField, OuterRef, Q, Subquery, Sum, Value
from django.db.models.functions import Coalesce
from django.http import HttpResponse
from django.utils import timezone
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.permissions import SAFE_METHODS
from rest_framework.response import Response

from accounts.serializers import UserBriefSerializer
from notifications import service as alerts

from . import activity
from .dashboard import build_dashboard
from .importer import import_tasks
from .workbook import WorkbookError, build_workbook, import_workbook
from .models import Activity, Comment, Invitation, Issue, Membership, Milestone, Project, Risk, Sprint, Task
from .permissions import DEVELOPER, MANAGER, OWNER, VIEWER, get_membership, get_project_for_user, require_rank
from .serializers import (
    ActivitySerializer, CommentSerializer, InvitationSerializer, IssueSerializer, MembershipSerializer,
    MilestoneSerializer, MyInvitationSerializer, ProjectSerializer, RiskSerializer, SprintSerializer, TaskSerializer,
)


TASK_ORDER = ["sprint__order", "sprint__start_date", "id"]  # within a sprint: sheet/creation order


def _int_subquery(qs, field):
    return Coalesce(Subquery(qs.values(field)[:1], output_field=IntegerField()), Value(0))


class ProjectViewSet(viewsets.ModelViewSet):
    serializer_class = ProjectSerializer

    def get_queryset(self):
        user = self.request.user
        tasks = Task.objects.filter(project=OuterRef("pk")).order_by().values("project")
        members = Membership.objects.filter(project=OuterRef("pk")).order_by().values("project")
        return (
            Project.objects.filter(memberships__user=user)
            .annotate(
                my_role=Subquery(Membership.objects.filter(project=OuterRef("pk"), user=user).values("role")[:1]),
                member_count=_int_subquery(members.annotate(c=Count("id")), "c"),
                task_count=_int_subquery(tasks.annotate(c=Count("id")), "c"),
                sp_total=_int_subquery(tasks.annotate(s=Sum("story_points")), "s"),
                sp_done=_int_subquery(tasks.filter(status=Task.Status.DONE).annotate(s=Sum("story_points")), "s"),
            )
        )

    def _require(self, project, rank):
        require_rank(get_membership(project, self.request.user), rank)

    @transaction.atomic
    def perform_create(self, serializer):
        project = serializer.save(created_by=self.request.user)
        Membership.objects.create(project=project, user=self.request.user, role=Membership.Role.OWNER)
        activity.log(project, self.request.user, "created", project)

    def perform_update(self, serializer):
        project = serializer.instance
        self._require(project, MANAGER)
        fields = ["name", "code", "description", "start_date", "end_date", "release_date"]
        before = activity.snapshot(project, fields)
        serializer.save()
        changes = activity.diff(before, activity.snapshot(project, fields))
        if changes:
            activity.log(project, self.request.user, "updated", project, changes=changes)

    def perform_destroy(self, instance):
        self._require(instance, OWNER)
        instance.delete()

    # ---- Excel workbook ------------------------------------------------------
    XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

    def _xlsx_response(self, data, filename):
        resp = HttpResponse(data, content_type=self.XLSX)
        resp["Content-Disposition"] = f'attachment; filename="{filename}"'
        return resp

    def _import_response(self, request, project=None):
        upload = request.FILES.get("file")
        if upload is None:
            raise ValidationError({"file": "Attach an Excel (.xlsx) file."})
        try:
            project, result = import_workbook(upload, request.user, project)
        except WorkbookError as e:
            return Response({"detail": "Nothing was imported. Fix these problems and try again.", "errors": e.errors},
                            status=status.HTTP_400_BAD_REQUEST)
        project = self.get_queryset().get(pk=project.pk)
        return Response({"project": ProjectSerializer(project, context={"request": request}).data, **result},
                        status=status.HTTP_201_CREATED if result["created_project"] else status.HTTP_200_OK)

    @action(detail=False, methods=["get"])
    def template(self, request):
        """Blank workbook to fill in and import as a new project."""
        return self._xlsx_response(build_workbook(), "project_template.xlsx")

    @action(detail=False, methods=["post"], url_path="import", parser_classes=[MultiPartParser, FormParser])
    def import_new(self, request):
        """Create a project from a filled-in workbook. The importer becomes its owner."""
        return self._import_response(request)

    @action(detail=True, methods=["get", "post"], parser_classes=[MultiPartParser, FormParser])
    def workbook(self, request, pk=None):
        """GET: the project as a workbook (any member). POST: update the project from one (managers)."""
        project = self.get_object()
        if request.method == "GET":
            name = (project.code or f"project-{project.id}").replace(" ", "_")
            return self._xlsx_response(build_workbook(project), f"{name}_workbook.xlsx")
        require_rank(get_membership(project, request.user), MANAGER, "Only project managers can import a workbook.")
        return self._import_response(request, project)

    @action(detail=True, methods=["get"])
    def dashboard(self, request, pk=None):
        return Response(build_dashboard(self.get_object()))

    @action(detail=True, methods=["get"])
    def activity(self, request, pk=None):
        project = self.get_object()
        qs = Activity.objects.filter(project=project).select_related("actor")
        before = request.query_params.get("before")
        if before and before.isdigit():
            qs = qs.filter(id__lt=int(before))
        try:
            limit = max(1, min(int(request.query_params.get("limit", 30)), 100))
        except ValueError:
            limit = 30
        return Response(ActivitySerializer(qs[:limit], many=True).data)

    @action(detail=True, methods=["get"])
    def report(self, request, pk=None):
        """Everything needed for the printable status report. Owners only."""
        project = self.get_object()
        require_rank(get_membership(project, request.user), OWNER, "Only project owners can generate the report.")

        ctx = {"request": request, "project": project}
        tasks = (
            Task.objects.filter(project=project)
            .select_related("assignee", "sprint", "milestone")
            .annotate(comment_count=Count("comments"))
            .order_by(*TASK_ORDER)  # Meta.ordering is skipped on aggregate queries
        )
        risks = (
            Risk.objects.filter(project=project, status__in=Risk.OPEN_STATUSES)
            .select_related("owner")
        )
        risks = sorted(risks, key=lambda r: (-r.score, r.id))
        issues = (
            Issue.objects.filter(project=project, status__in=Issue.OPEN_STATUSES)
            .select_related("owner", "reported_by", "task")
        )
        members = Membership.objects.filter(project=project).select_related("user")
        activity.log(project, request.user, "generated", entity_type="report", label="Project status report")
        return Response({
            "generated_at": timezone.now(),
            "generated_by": UserBriefSerializer(request.user).data,
            "dashboard": build_dashboard(project),
            "tasks": TaskSerializer(tasks, many=True, context=ctx).data,
            "open_risks": RiskSerializer(risks, many=True, context=ctx).data,
            "open_issues": IssueSerializer(issues, many=True, context=ctx).data,
            "members": MembershipSerializer(members, many=True, context=ctx).data,
        })


class ProjectScopedViewSet(viewsets.ModelViewSet):
    """Base for /api/projects/<project_pk>/<resource>/ endpoints."""

    write_rank = DEVELOPER   # create / update
    delete_rank = MANAGER
    tracked_fields: list[str] = []

    def initial(self, request, *args, **kwargs):
        super().initial(request, *args, **kwargs)
        self.project = get_project_for_user(kwargs["project_pk"], request.user)
        self.membership = get_membership(self.project, request.user)
        if request.method not in SAFE_METHODS:
            require_rank(self.membership, self.delete_rank if request.method == "DELETE" else self.write_rank)

    def get_serializer_context(self):
        ctx = super().get_serializer_context()
        if hasattr(self, "project"):
            ctx["project"] = self.project
        return ctx

    def create_kwargs(self):
        return {}

    def perform_create(self, serializer):
        obj = serializer.save(project=self.project, **self.create_kwargs())
        activity.log(self.project, self.request.user, "created", obj)

    def perform_update(self, serializer):
        before = activity.snapshot(serializer.instance, self.tracked_fields)
        obj = serializer.save()
        changes = activity.diff(before, activity.snapshot(obj, self.tracked_fields))
        if changes:
            activity.log(self.project, self.request.user, "updated", obj, changes=changes)

    def perform_destroy(self, instance):
        activity.log(self.project, self.request.user, "deleted", instance)
        instance.delete()


def _sp_annotations():
    return {
        "sp_total": Coalesce(Sum("tasks__story_points"), 0),
        "sp_done": Coalesce(Sum("tasks__story_points", filter=Q(tasks__status=Task.Status.DONE)), 0),
        "task_count": Count("tasks"),
    }


class SprintViewSet(ProjectScopedViewSet):
    serializer_class = SprintSerializer
    write_rank = MANAGER
    tracked_fields = ["name", "order", "start_date", "end_date", "goal"]

    def get_queryset(self):
        return Sprint.objects.filter(project=self.project).annotate(**_sp_annotations())


class MilestoneViewSet(ProjectScopedViewSet):
    serializer_class = MilestoneSerializer
    write_rank = MANAGER
    tracked_fields = ["name", "due_date", "sprint", "is_done"]

    def get_queryset(self):
        return Milestone.objects.filter(project=self.project).select_related("sprint").annotate(**_sp_annotations())

    def perform_update(self, serializer):
        was_done = serializer.instance.is_done
        super().perform_update(serializer)
        if serializer.instance.is_done and not was_done:
            alerts.on_milestone_done(serializer.instance, self.request.user)


class TaskViewSet(ProjectScopedViewSet):
    serializer_class = TaskSerializer
    tracked_fields = [
        "code", "title", "sprint", "milestone", "assignee", "story_points", "status", "percent_done",
        "priority", "start_date", "due_date",
    ]
    parser_classes = [JSONParser, MultiPartParser, FormParser]

    def get_queryset(self):
        qs = (
            Task.objects.filter(project=self.project)
            .select_related("assignee", "sprint", "milestone")
            .annotate(comment_count=Count("comments"))
            .order_by(*TASK_ORDER)  # Meta.ordering is skipped on aggregate queries
        )
        p = self.request.query_params
        if p.get("sprint"):
            qs = qs.filter(sprint__isnull=True) if p["sprint"] == "none" else qs.filter(sprint_id=p["sprint"])
        if p.get("assignee"):
            a = p["assignee"]
            if a == "me":
                qs = qs.filter(assignee=self.request.user)
            elif a == "none":
                qs = qs.filter(assignee__isnull=True)
            else:
                qs = qs.filter(assignee_id=a)
        if p.get("status"):
            qs = qs.filter(status__in=p["status"].split(","))
        if p.get("milestone"):
            qs = qs.filter(milestone_id=p["milestone"])
        if p.get("search"):
            s = p["search"]
            qs = qs.filter(Q(title__icontains=s) | Q(code__icontains=s) | Q(description__icontains=s))
        return qs

    def create_kwargs(self):
        return {"created_by": self.request.user}

    def perform_create(self, serializer):
        super().perform_create(serializer)
        alerts.on_task_created(serializer.instance, self.request.user)

    def perform_update(self, serializer):
        before = {"assignee_id": serializer.instance.assignee_id, "status": serializer.instance.status}
        super().perform_update(serializer)
        alerts.on_task_updated(serializer.instance, before, self.request.user)

    @action(detail=False, methods=["get"])
    def export(self, request, project_pk=None):
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(["Code", "Task", "Sprint", "Owner", "SP", "Status", "% Done", "Priority",
                         "Start", "Due", "Milestone", "Description"])
        for t in self.get_queryset():
            writer.writerow([
                t.code, t.title, t.sprint.name if t.sprint else "",
                t.assignee.username if t.assignee else "", t.story_points, t.get_status_display(),
                t.percent_done, t.get_priority_display(), t.start_date or "", t.due_date or "",
                t.milestone.name if t.milestone else "", t.description,
            ])
        resp = HttpResponse(buf.getvalue(), content_type="text/csv")
        name = (self.project.code or f"project-{self.project.id}").replace(" ", "_")
        resp["Content-Disposition"] = f'attachment; filename="{name}_tasks.csv"'
        return resp

    @action(detail=False, methods=["post"], url_path="import")
    def import_csv(self, request, project_pk=None):
        require_rank(self.membership, MANAGER, "Only project managers can import tasks.")
        upload = request.FILES.get("file")
        if upload is None:
            raise ValidationError({"file": "Attach a CSV file."})
        try:
            text = upload.read().decode("utf-8-sig")
        except UnicodeDecodeError:
            raise ValidationError({"file": "File must be UTF-8 encoded CSV."})
        result = import_tasks(self.project, text, request.user)
        if result["created"] or result["updated"]:
            activity.log(
                self.project, request.user, "imported", entity_type="task",
                label=f"{result['created']} created, {result['updated']} updated from CSV",
            )
        return Response(result, status=status.HTTP_200_OK)


class CommentViewSet(ProjectScopedViewSet):
    serializer_class = CommentSerializer
    write_rank = VIEWER   # everyone in the project can discuss
    delete_rank = VIEWER  # own comments; managers can delete any (checked below)

    def get_queryset(self):
        qs = Comment.objects.filter(task__project=self.project).select_related("author")
        task = self.request.query_params.get("task")
        if task:
            qs = qs.filter(task_id=task)
        return qs

    def perform_create(self, serializer):
        comment = serializer.save(author=self.request.user)
        activity.log(self.project, self.request.user, "commented", comment.task,
                     changes={"comment": comment.body[:200]})
        alerts.on_comment(comment, self.request.user)

    def perform_update(self, serializer):
        if serializer.instance.author_id != self.request.user.id:
            raise PermissionDenied("You can only edit your own comments.")
        serializer.save()

    def perform_destroy(self, instance):
        if instance.author_id != self.request.user.id:
            require_rank(self.membership, MANAGER, "You can only delete your own comments.")
        instance.delete()


class RiskViewSet(ProjectScopedViewSet):
    serializer_class = RiskSerializer
    tracked_fields = ["title", "probability", "impact", "status", "owner"]

    def get_queryset(self):
        qs = Risk.objects.filter(project=self.project).select_related("owner")
        if self.request.query_params.get("open") == "1":
            qs = qs.filter(status__in=Risk.OPEN_STATUSES)
        return qs


class IssueViewSet(ProjectScopedViewSet):
    serializer_class = IssueSerializer
    tracked_fields = ["title", "severity", "status", "owner", "task"]

    def get_queryset(self):
        qs = Issue.objects.filter(project=self.project).select_related("owner", "reported_by", "task")
        if self.request.query_params.get("open") == "1":
            qs = qs.filter(status__in=Issue.OPEN_STATUSES)
        return qs

    def create_kwargs(self):
        return {"reported_by": self.request.user}


class MembershipViewSet(ProjectScopedViewSet):
    serializer_class = MembershipSerializer
    write_rank = MANAGER
    delete_rank = VIEWER  # anyone may leave; removing others is checked below
    # New members are invited (see InvitationViewSet), not added directly.
    http_method_names = ["get", "patch", "delete", "head", "options"]

    def get_queryset(self):
        return Membership.objects.filter(project=self.project).select_related("user")

    def _owner_count(self):
        return Membership.objects.filter(project=self.project, role=Membership.Role.OWNER).count()

    def perform_update(self, serializer):
        m = serializer.instance
        new_role = serializer.validated_data.get("role", m.role)
        if Membership.Role.OWNER in (m.role, new_role) and new_role != m.role:
            require_rank(self.membership, OWNER, "Only owners can change owner roles.")
            if m.role == Membership.Role.OWNER and self._owner_count() <= 1:
                raise ValidationError({"role": "A project needs at least one owner."})
        old = m.role
        serializer.save()
        if old != new_role:
            activity.log(self.project, self.request.user, "updated", m, entity_type="member",
                         label=m.user.get_full_name() or m.user.username, changes={"role": [old, new_role]})

    def perform_destroy(self, instance):
        is_self = instance.user_id == self.request.user.id
        if not is_self:
            require_rank(self.membership, MANAGER, "Only project managers can remove members.")
            if instance.role == Membership.Role.OWNER:
                require_rank(self.membership, OWNER, "Only owners can remove an owner.")
        if instance.role == Membership.Role.OWNER and self._owner_count() <= 1:
            raise ValidationError({"detail": "A project needs at least one owner. Transfer ownership first."})
        activity.log(self.project, self.request.user, "left" if is_self else "removed", instance,
                     entity_type="member", label=instance.user.get_full_name() or instance.user.username)
        instance.delete()


def _user_label(user):
    return user.get_full_name() or user.username


class InvitationViewSet(ProjectScopedViewSet):
    """Pending invitations of a project. Managers invite people and can withdraw invitations."""

    serializer_class = InvitationSerializer
    write_rank = MANAGER
    delete_rank = MANAGER
    http_method_names = ["get", "post", "delete", "head", "options"]

    def get_queryset(self):
        return Invitation.objects.filter(project=self.project).select_related("user", "invited_by")

    @transaction.atomic
    def perform_create(self, serializer):
        if serializer.validated_data.get("role") == Membership.Role.OWNER:
            require_rank(self.membership, OWNER, "Only owners can invite another owner.")
        inv = serializer.save(project=self.project, invited_by=self.request.user)
        alerts.on_invited(inv, self.request.user)
        activity.log(self.project, self.request.user, "invited", inv, entity_type="member",
                     label=_user_label(inv.user), changes={"role": inv.role})

    @transaction.atomic
    def perform_destroy(self, instance):
        alerts.clear_invite_alerts(instance)
        activity.log(self.project, self.request.user, "withdrew invitation for", instance, entity_type="member",
                     label=_user_label(instance.user))
        instance.delete()


class MyInvitationViewSet(viewsets.ReadOnlyModelViewSet):
    """/api/invitations/ — the current user's pending invitations, which they can accept or decline."""

    serializer_class = MyInvitationSerializer

    def get_queryset(self):
        return Invitation.objects.filter(user=self.request.user).select_related("project", "invited_by")

    @action(detail=True, methods=["post"])
    @transaction.atomic
    def accept(self, request, pk=None):
        inv = self.get_object()
        project = inv.project
        m, _ = Membership.objects.get_or_create(project=project, user=request.user, defaults={"role": inv.role})
        alerts.clear_invite_alerts(inv)
        alerts.on_invite_answered(inv, accepted=True)
        activity.log(project, request.user, "joined", m, entity_type="project",
                     label=project.name, changes={"role": m.role})
        inv.delete()
        project = ProjectViewSet(request=request).get_queryset().get(pk=project.pk)
        return Response(ProjectSerializer(project, context={"request": request}).data)

    @action(detail=True, methods=["post"])
    @transaction.atomic
    def decline(self, request, pk=None):
        inv = self.get_object()
        alerts.clear_invite_alerts(inv)
        alerts.on_invite_answered(inv, accepted=False)
        activity.log(inv.project, request.user, "declined invitation to", inv, entity_type="project",
                     label=inv.project.name)
        inv.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)
