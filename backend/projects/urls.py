from rest_framework.routers import DefaultRouter

from . import views

P = r"projects/(?P<project_pk>\d+)"

router = DefaultRouter()
router.register(r"projects", views.ProjectViewSet, basename="project")
router.register(r"invitations", views.MyInvitationViewSet, basename="my-invitation")
router.register(rf"{P}/members", views.MembershipViewSet, basename="member")
router.register(rf"{P}/invitations", views.InvitationViewSet, basename="invitation")
router.register(rf"{P}/sprints", views.SprintViewSet, basename="sprint")
router.register(rf"{P}/milestones", views.MilestoneViewSet, basename="milestone")
router.register(rf"{P}/tasks", views.TaskViewSet, basename="task")
router.register(rf"{P}/comments", views.CommentViewSet, basename="comment")
router.register(rf"{P}/risks", views.RiskViewSet, basename="risk")
router.register(rf"{P}/issues", views.IssueViewSet, basename="issue")

urlpatterns = router.urls
