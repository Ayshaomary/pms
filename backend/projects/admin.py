from django.contrib import admin

from .models import Activity, Comment, Invitation, Issue, Membership, Milestone, Project, Risk, Sprint, Task


class MembershipInline(admin.TabularInline):
    model = Membership
    extra = 0


@admin.register(Project)
class ProjectAdmin(admin.ModelAdmin):
    list_display = ["name", "code", "start_date", "end_date", "release_date"]
    inlines = [MembershipInline]


@admin.register(Task)
class TaskAdmin(admin.ModelAdmin):
    list_display = ["code", "title", "project", "sprint", "assignee", "story_points", "status", "percent_done"]
    list_filter = ["project", "status", "sprint"]
    search_fields = ["code", "title"]


for model in [Invitation, Sprint, Milestone, Risk, Issue, Comment, Activity]:
    admin.site.register(model)
