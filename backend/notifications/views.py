from rest_framework import serializers, status
from rest_framework.decorators import api_view
from rest_framework.generics import get_object_or_404
from rest_framework.response import Response

from accounts.serializers import UserBriefSerializer

from . import service
from .models import Notification


class NotificationSerializer(serializers.ModelSerializer):
    actor = UserBriefSerializer(read_only=True)
    project_name = serializers.CharField(source="project.name", read_only=True)

    class Meta:
        model = Notification
        fields = ["id", "kind", "text", "actor", "project", "project_name", "task", "milestone", "is_read", "created_at"]


@api_view(["GET"])
def notification_list(request):
    # Overdue alerts are time-based; generate any that are due for this user now.
    service.check_overdue(user=request.user)
    qs = service.visible_to(request.user)
    if request.query_params.get("unread") == "1":
        qs = qs.filter(is_read=False)
    try:
        limit = max(1, min(int(request.query_params.get("limit", 20)), 100))
    except ValueError:
        limit = 20
    return Response({
        "unread": service.visible_to(request.user).filter(is_read=False).count(),
        "results": NotificationSerializer(qs[:limit], many=True).data,
    })


@api_view(["GET"])
def unread_count(request):
    service.check_overdue(user=request.user)
    qs = service.visible_to(request.user).filter(is_read=False)
    latest = qs.first()
    return Response({
        "unread": qs.count(),
        "latest": NotificationSerializer(latest).data if latest else None,
    })


@api_view(["POST"])
def mark_read(request, pk):
    n = get_object_or_404(Notification, pk=pk, recipient=request.user)
    if not n.is_read:
        n.is_read = True
        n.save(update_fields=["is_read"])
    return Response(status=status.HTTP_204_NO_CONTENT)


@api_view(["POST"])
def mark_all_read(request):
    Notification.objects.filter(recipient=request.user, is_read=False).update(is_read=True)
    return Response(status=status.HTTP_204_NO_CONTENT)
