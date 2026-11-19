"""Role-based access inside a project.

viewer     read everything, comment on tasks
developer  + create/edit tasks, risks, issues
manager    + sprints, milestones, members, project settings, deletes
owner      + delete project, promote/demote owners
"""

from django.shortcuts import get_object_or_404
from rest_framework.exceptions import PermissionDenied

from .models import Membership, Project

VIEWER, DEVELOPER, MANAGER, OWNER = 0, 1, 2, 3


def get_membership(project, user):
    if not user.is_authenticated:
        return None
    return Membership.objects.filter(project=project, user=user).first()


def get_project_for_user(project_id, user):
    """Return the project if the user is a member; 404 otherwise (don't leak existence)."""
    return get_object_or_404(Project, pk=project_id, memberships__user=user)


def require_rank(membership, rank, message="You don't have permission to do this in this project."):
    if membership is None or membership.rank < rank:
        raise PermissionDenied(message)
