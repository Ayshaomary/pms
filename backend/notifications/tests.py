from datetime import date, timedelta

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase
from rest_framework.test import APIClient

from projects.models import Membership, Project, Task

from .models import Notification

User = get_user_model()


class AlertTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user("owner", "o@example.com", "pw", first_name="Olivia", last_name="Owner")
        self.mgr = User.objects.create_user("mgr", "m@example.com", "pw")
        self.dev = User.objects.create_user("dev", "d@example.com", "pw")
        self.dev2 = User.objects.create_user("dev2", "d2@example.com", "pw")
        self.outsider = User.objects.create_user("outsider", "x@example.com", "pw")
        self.project = Project.objects.create(name="PES")
        for u, role in [(self.owner, "owner"), (self.mgr, "manager"), (self.dev, "developer"), (self.dev2, "developer")]:
            Membership.objects.create(project=self.project, user=u, role=role)
        self.pid = self.project.id

    def api(self, user):
        c = APIClient()
        c.force_authenticate(user)
        return c

    def url(self, path):
        return f"/api/projects/{self.pid}/{path}"

    def kinds(self, user):
        return list(Notification.objects.filter(recipient=user).values_list("kind", flat=True))

    def test_assignment_alerts_assignee_but_not_self(self):
        t = self.api(self.owner).post(self.url("tasks/"), {"title": "Login", "code": "FR-1", "assignee": self.dev.id}, format="json").json()
        self.assertEqual(self.kinds(self.dev), ["assigned"])
        n = Notification.objects.get(recipient=self.dev)
        self.assertIn("Olivia Owner assigned you FR-1 Login", n.text)
        self.assertEqual(n.task_id, t["id"])

        # Self-assignment: no alert. Reassignment: new assignee alerted once.
        self.api(self.dev2).post(self.url("tasks/"), {"title": "Mine", "assignee": self.dev2.id}, format="json")
        self.assertEqual(self.kinds(self.dev2), [])
        self.api(self.owner).patch(self.url(f"tasks/{t['id']}/"), {"assignee": self.dev2.id}, format="json")
        self.api(self.owner).patch(self.url(f"tasks/{t['id']}/"), {"title": "Login page"}, format="json")
        self.assertEqual(self.kinds(self.dev2), ["assigned"])

    def test_blocked_alerts_managers_and_assignee(self):
        t = self.api(self.owner).post(self.url("tasks/"), {"title": "T", "assignee": self.dev.id}, format="json").json()
        Notification.objects.all().delete()
        self.api(self.dev).patch(self.url(f"tasks/{t['id']}/"), {"status": "blocked"}, format="json")
        self.assertEqual(self.kinds(self.owner), ["blocked"])
        self.assertEqual(self.kinds(self.mgr), ["blocked"])
        self.assertEqual(self.kinds(self.dev), [])      # the one who blocked it
        self.assertEqual(self.kinds(self.dev2), [])
        # Staying blocked doesn't re-alert.
        self.api(self.dev).patch(self.url(f"tasks/{t['id']}/"), {"percent_done": 20}, format="json")
        self.assertEqual(len(self.kinds(self.owner)), 1)

    def test_mentions_and_comment_on_your_task(self):
        t = self.api(self.owner).post(self.url("tasks/"), {"title": "T", "assignee": self.dev.id}, format="json").json()
        Notification.objects.all().delete()
        self.api(self.mgr).post(self.url("comments/"), {"task": t["id"], "body": "@dev2 can you pair with @Owner? cc @outsider."}, format="json")
        self.assertEqual(self.kinds(self.dev2), ["mentioned"])
        self.assertEqual(self.kinds(self.owner), ["mentioned"])   # case-insensitive
        self.assertEqual(self.kinds(self.outsider), [])           # not a member
        self.assertEqual(self.kinds(self.dev), ["commented"])     # assignee, not mentioned
        self.assertEqual(self.kinds(self.mgr), [])                # author

        # Mentioned assignee gets a single "mentioned" alert, not both.
        self.api(self.mgr).post(self.url("comments/"), {"task": t["id"], "body": "@dev please check"}, format="json")
        self.assertEqual(self.kinds(self.dev), ["mentioned", "commented"])

    def test_milestone_done_alerts_team_once(self):
        ms = self.api(self.owner).post(self.url("milestones/"), {"name": "Beta"}, format="json").json()
        self.api(self.owner).patch(self.url(f"milestones/{ms['id']}/"), {"is_done": True}, format="json")
        self.api(self.owner).patch(self.url(f"milestones/{ms['id']}/"), {"description": "x"}, format="json")
        for u in (self.mgr, self.dev, self.dev2):
            self.assertEqual(self.kinds(u), ["milestone_done"])
        self.assertEqual(self.kinds(self.owner), [])

    def test_invitation_alerts(self):
        inv = self.api(self.owner).post(self.url("invitations/"), {"identifier": "outsider", "role": "viewer"},
                                        format="json").json()
        n = Notification.objects.get(recipient=self.outsider)
        self.assertEqual(n.kind, "invited")
        self.assertIn("invited you to PES as Viewer", n.text)
        # Visible to the invitee even though they aren't a member yet.
        listed = self.api(self.outsider).get("/api/notifications/").json()
        self.assertEqual([x["kind"] for x in listed["results"]], ["invited"])

        self.api(self.outsider).post(f"/api/invitations/{inv['id']}/accept/")
        self.assertEqual(self.kinds(self.outsider), [])  # invite alert is cleared once answered
        self.assertEqual(self.kinds(self.owner), ["invite_accepted"])

    def test_declined_invitation_alerts_inviter(self):
        inv = self.api(self.mgr).post(self.url("invitations/"), {"identifier": "outsider"}, format="json").json()
        self.api(self.outsider).post(f"/api/invitations/{inv['id']}/decline/")
        self.assertEqual(self.kinds(self.outsider), [])
        self.assertEqual(self.kinds(self.mgr), ["invite_declined"])
        self.assertEqual(self.kinds(self.owner), [])

    def test_overdue_created_once_and_lazily(self):
        yesterday = date.today() - timedelta(days=1)
        Task.objects.create(project=self.project, title="Late", assignee=self.dev, due_date=yesterday)
        Task.objects.create(project=self.project, title="Done late", assignee=self.dev, due_date=yesterday, status="done")
        c = self.api(self.dev)
        self.assertEqual(c.get("/api/notifications/unread-count/").json()["unread"], 1)
        c.get("/api/notifications/")
        call_command("send_overdue_alerts", stdout=open("/dev/null", "w"))
        self.assertEqual(self.kinds(self.dev), ["overdue"])

    def test_list_read_and_privacy(self):
        self.api(self.owner).post(self.url("tasks/"), {"title": "A", "assignee": self.dev.id}, format="json")
        self.api(self.owner).post(self.url("tasks/"), {"title": "B", "assignee": self.dev.id}, format="json")
        c = self.api(self.dev)
        body = c.get("/api/notifications/").json()
        self.assertEqual(body["unread"], 2)
        self.assertEqual(body["results"][0]["project_name"], "PES")
        first = body["results"][0]["id"]

        # Others can't read or mark my notifications.
        self.assertEqual(self.api(self.dev2).post(f"/api/notifications/{first}/read/").status_code, 404)
        self.assertEqual(self.api(self.dev2).get("/api/notifications/").json()["results"], [])

        self.assertEqual(c.post(f"/api/notifications/{first}/read/").status_code, 204)
        self.assertEqual(c.get("/api/notifications/unread-count/").json()["unread"], 1)
        c.post("/api/notifications/read-all/")
        self.assertEqual(c.get("/api/notifications/?unread=1").json()["results"], [])

        # Removed from the project -> its alerts disappear from my list.
        Membership.objects.filter(project=self.project, user=self.dev).delete()
        self.assertEqual(c.get("/api/notifications/").json()["results"], [])

    def test_anonymous_rejected(self):
        self.assertEqual(APIClient().get("/api/notifications/").status_code, 403)
