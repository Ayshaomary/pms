from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.test import TestCase
from rest_framework.test import APIClient

from .models import Membership, Project, Task

User = get_user_model()


class AuthFlowTests(TestCase):
    def test_register_login_me_logout_with_csrf(self):
        c = APIClient(enforce_csrf_checks=True)
        c.get("/api/auth/csrf/")
        token = c.cookies["csrftoken"].value
        r = c.post("/api/auth/register/", {"username": "amina", "email": "amina@example.com",
                                           "password": "Str0ng-pass-99"}, format="json",
                   HTTP_X_CSRFTOKEN=token)
        self.assertEqual(r.status_code, 201, r.content)
        self.assertEqual(c.get("/api/auth/me/").json()["username"], "amina")

        # Authenticated writes without the CSRF header are rejected.
        self.assertEqual(c.post("/api/projects/", {"name": "X"}, format="json").status_code, 403)
        token = c.cookies["csrftoken"].value
        self.assertEqual(c.post("/api/projects/", {"name": "X"}, format="json",
                                HTTP_X_CSRFTOKEN=token).status_code, 201)

        c.post("/api/auth/logout/", HTTP_X_CSRFTOKEN=token)
        self.assertEqual(c.get("/api/auth/me/").status_code, 403)

    def test_login_with_email(self):
        User.objects.create_user("bob", "bob@example.com", "Str0ng-pass-99")
        r = APIClient().post("/api/auth/login/", {"username": "BOB@example.com", "password": "Str0ng-pass-99"},
                             format="json")
        self.assertEqual(r.status_code, 200)


class ProjectTestBase(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user("owner", "o@example.com", "pw")
        self.dev = User.objects.create_user("dev", "d@example.com", "pw")
        self.viewer = User.objects.create_user("viewer", "v@example.com", "pw")
        self.outsider = User.objects.create_user("outsider", "x@example.com", "pw")
        self.client_for = {}
        c = self.api(self.owner)
        self.pid = c.post("/api/projects/", {"name": "PES", "code": "PES"}, format="json").json()["id"]
        self.join(self.dev, "dev", "developer")
        self.join(self.viewer, "v@example.com", "viewer")

    def join(self, user, identifier, role):
        """Invite `user` as the owner, then accept as `user`."""
        r = self.api(self.owner).post(f"/api/projects/{self.pid}/invitations/",
                                      {"identifier": identifier, "role": role}, format="json")
        assert r.status_code == 201, r.content
        r = self.api(user).post(f"/api/invitations/{r.json()['id']}/accept/")
        assert r.status_code == 200, r.content

    def api(self, user):
        c = APIClient()
        c.force_authenticate(user)
        return c

    def url(self, path=""):
        return f"/api/projects/{self.pid}/{path}"


class PermissionTests(ProjectTestBase):
    def test_creator_is_owner_and_members_listed(self):
        project = Project.objects.get(pk=self.pid)
        self.assertEqual(Membership.objects.get(project=project, user=self.owner).role, "owner")
        self.assertEqual(len(self.api(self.dev).get(self.url("members/")).json()), 3)

    def test_outsider_cannot_see_project(self):
        c = self.api(self.outsider)
        self.assertEqual(c.get("/api/projects/").json(), [])
        self.assertEqual(c.get(self.url("dashboard/")).status_code, 404)
        self.assertEqual(c.get(self.url("tasks/")).status_code, 404)

    def test_viewer_reads_and_comments_but_cannot_edit(self):
        task = self.api(self.dev).post(self.url("tasks/"), {"title": "T", "story_points": 3}, format="json").json()
        v = self.api(self.viewer)
        self.assertEqual(v.get(self.url("tasks/")).status_code, 200)
        self.assertEqual(v.post(self.url("tasks/"), {"title": "nope"}, format="json").status_code, 403)
        self.assertEqual(v.patch(self.url(f"tasks/{task['id']}/"), {"status": "done"}, format="json").status_code, 403)
        self.assertEqual(v.post(self.url("comments/"), {"task": task["id"], "body": "hi"}, format="json").status_code, 201)

    def test_developer_cannot_manage_sprints_or_members(self):
        d = self.api(self.dev)
        self.assertEqual(d.post(self.url("sprints/"), {"name": "S1"}, format="json").status_code, 403)
        self.assertEqual(d.post(self.url("invitations/"), {"identifier": "outsider"}, format="json").status_code, 403)

    def test_report_is_owner_only(self):
        self.api(self.dev).post(self.url("tasks/"), {"title": "T", "story_points": 3}, format="json")
        r = self.api(self.owner).get(self.url("report/"))
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["dashboard"]["key_figures"]["total_sp"], 3)
        self.assertEqual(len(body["tasks"]), 1)
        self.assertEqual(len(body["members"]), 3)
        self.assertEqual(body["generated_by"]["username"], "owner")

        manager = User.objects.create_user("mgr", "m@example.com", "pw")
        Membership.objects.create(project_id=self.pid, user=manager, role="manager")
        self.assertEqual(self.api(manager).get(self.url("report/")).status_code, 403)
        self.assertEqual(self.api(self.dev).get(self.url("report/")).status_code, 403)
        self.assertEqual(self.api(self.viewer).get(self.url("report/")).status_code, 403)
        self.assertEqual(self.api(self.outsider).get(self.url("report/")).status_code, 404)

    def test_assignee_must_be_member(self):
        r = self.api(self.owner).post(self.url("tasks/"), {"title": "T", "assignee": self.outsider.id}, format="json")
        self.assertEqual(r.status_code, 400)

    def test_last_owner_cannot_leave(self):
        c = self.api(self.owner)
        mid = Membership.objects.get(project_id=self.pid, user=self.owner).id
        self.assertEqual(c.delete(self.url(f"members/{mid}/")).status_code, 400)
        self.assertEqual(c.patch(self.url(f"members/{mid}/"), {"role": "manager"}, format="json").status_code, 400)


class TaskTests(ProjectTestBase):
    def test_done_sets_percent_and_logs_activity(self):
        c = self.api(self.dev)
        t = c.post(self.url("tasks/"), {"title": "T", "story_points": 5}, format="json").json()
        r = c.patch(self.url(f"tasks/{t['id']}/"), {"status": "done"}, format="json").json()
        self.assertEqual(r["percent_done"], 100)
        self.assertIsNotNone(r["completed_at"])
        r = c.patch(self.url(f"tasks/{t['id']}/"), {"status": "in_progress"}, format="json").json()
        self.assertEqual(r["percent_done"], 99)
        self.assertIsNone(r["completed_at"])
        # 100% without a status marks done
        r = c.patch(self.url(f"tasks/{t['id']}/"), {"percent_done": 100}, format="json").json()
        self.assertEqual(r["status"], "done")
        feed = c.get(self.url("activity/")).json()
        self.assertEqual(feed[0]["verb"], "updated")
        self.assertIn("status", feed[0]["changes"])

    def test_import_csv_with_title_rows(self):
        csv_text = (
            "PES TASK TRACKER,,,,,,\n"
            ",,,,,,\n"
            "ID,Task,Sprint,Owner,SP,Status,% Done\n"
            "FR-1.1,Login page,S1,dev,5,In Progress,40%\n"
            "FR-1.2,Signup page,S1,Ghost,3,Done,\n"
            ",,,,,,\n"
        )
        c = self.api(self.owner)
        upload = SimpleUploadedFile("t.csv", csv_text.encode(), content_type="text/csv")
        r = c.post(self.url("tasks/import/"), {"file": upload}, format="multipart").json()
        self.assertEqual((r["created"], r["updated"]), (2, 0))
        self.assertEqual(len(r["warnings"]), 1)  # Ghost isn't a member
        t = Task.objects.get(project_id=self.pid, code="FR-1.1")
        self.assertEqual((t.assignee, t.percent_done, t.status, t.sprint.name), (self.dev, 40, "in_progress", "S1"))
        self.assertEqual(Task.objects.get(code="FR-1.2").status, "done")

        # Re-import updates by code
        upload = SimpleUploadedFile("t.csv", csv_text.replace("40%", "60%").encode(), content_type="text/csv")
        r = c.post(self.url("tasks/import/"), {"file": upload}, format="multipart").json()
        self.assertEqual((r["created"], r["updated"]), (0, 2))
        self.assertEqual(Task.objects.get(code="FR-1.1").percent_done, 60)

        export = c.get(self.url("tasks/export/"))
        self.assertIn("FR-1.1,Login page,S1,dev,5,In progress,60", export.content.decode())


class SeedDashboardTests(TestCase):
    def test_seed_matches_tracker_key_figures(self):
        call_command("seed_pes", password="x", stdout=open("/dev/null", "w"))
        pm = User.objects.get(username="pm")
        c = APIClient()
        c.force_authenticate(pm)
        pid = Project.objects.get(code="PES").id
        d = c.get(f"/api/projects/{pid}/dashboard/").json()

        kf = d["key_figures"]
        self.assertEqual((kf["total_sp"], kf["tasks"], kf["developers"], kf["sprints_label"], kf["release_date"]),
                         (278, 51, 4, "7 (incl. S0)", "2026-12-30"))
        lp = d["live_progress"]
        self.assertEqual((lp["sp_done"], lp["percent_complete"], lp["open_risks"], lp["open_issues"]), (0, 0.0, 12, 6))

        wl = d["workload"]
        self.assertEqual(sum(r["total"] for r in wl["rows"]), 278)
        self.assertAlmostEqual(sum(r["percent_of_total"] for r in wl["rows"]), 100.0, delta=0.2)
        for row in wl["rows"]:
            self.assertEqual(sum(row["by_sprint"].values()), row["total"])
        self.assertEqual(d["milestones"]["next_open"]["name"], "Sprint 0 setup complete")
        self.assertEqual(len(d["burndown"]), 45)

        # Completing a task moves live progress.
        task = Task.objects.filter(project_id=pid).first()
        dev = task.assignee
        dc = APIClient()
        dc.force_authenticate(dev)
        dc.patch(f"/api/projects/{pid}/tasks/{task.id}/", {"status": "done"}, format="json")
        d = c.get(f"/api/projects/{pid}/dashboard/").json()
        self.assertEqual(d["live_progress"]["sp_done"], task.story_points)
        self.assertEqual(d["live_progress"]["tasks_done"], 1)


class OrderingAndLabelTests(ProjectTestBase):
    def test_tasks_sorted_by_sprint_and_issue_without_task(self):
        c = self.api(self.owner)
        s1 = c.post(self.url("sprints/"), {"name": "S1", "order": 1}, format="json").json()["id"]
        s0 = c.post(self.url("sprints/"), {"name": "S0", "order": 0}, format="json").json()["id"]
        c.post(self.url("tasks/"), {"title": "late", "sprint": s1}, format="json")
        c.post(self.url("tasks/"), {"title": "early", "sprint": s0}, format="json")
        self.assertEqual([t["title"] for t in c.get(self.url("tasks/")).json()], ["early", "late"])
        self.assertEqual([t["title"] for t in c.get(self.url("report/")).json()["tasks"]], ["early", "late"])

        c.post(self.url("issues/"), {"title": "No task"}, format="json")
        self.assertIsNone(c.get(self.url("issues/")).json()[0]["task_label"])


class InvitationTests(ProjectTestBase):
    def invite(self, identifier="outsider", role="developer", by=None):
        return self.api(by or self.owner).post(self.url("invitations/"), {"identifier": identifier, "role": role},
                                               format="json")

    def test_invitee_has_no_access_until_accepting(self):
        inv = self.invite().json()
        out = self.api(self.outsider)
        self.assertEqual(out.get(self.url()).status_code, 404)
        self.assertEqual(out.get("/api/projects/").json(), [])
        mine = out.get("/api/invitations/").json()
        self.assertEqual([(i["project_name"], i["role"]) for i in mine], [("PES", "developer")])

        r = out.post(f"/api/invitations/{inv['id']}/accept/")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["my_role"], "developer")
        self.assertEqual(out.get(self.url()).status_code, 200)
        self.assertEqual(out.get("/api/invitations/").json(), [])
        self.assertTrue(Membership.objects.filter(project_id=self.pid, user=self.outsider, role="developer").exists())

    def test_decline_removes_invitation_without_joining(self):
        inv = self.invite().json()
        out = self.api(self.outsider)
        self.assertEqual(out.post(f"/api/invitations/{inv['id']}/decline/").status_code, 204)
        self.assertEqual(out.get("/api/invitations/").json(), [])
        self.assertEqual(out.get(self.url()).status_code, 404)
        self.assertFalse(Membership.objects.filter(project_id=self.pid, user=self.outsider).exists())
        self.assertEqual(self.invite().status_code, 201)  # can be invited again

    def test_only_invitee_can_answer(self):
        inv = self.invite().json()
        self.assertEqual(self.api(self.dev).post(f"/api/invitations/{inv['id']}/accept/").status_code, 404)
        self.assertEqual(self.api(self.owner).post(f"/api/invitations/{inv['id']}/decline/").status_code, 404)

    def test_invite_validation(self):
        self.assertEqual(self.invite("dev").status_code, 400)          # already a member
        self.assertEqual(self.invite().status_code, 201)
        self.assertEqual(self.invite().status_code, 400)               # already invited
        self.assertEqual(self.invite("nobody").status_code, 400)

    def test_managers_list_and_withdraw(self):
        inv = self.invite().json()
        self.assertEqual(len(self.api(self.owner).get(self.url("invitations/")).json()), 1)
        self.assertEqual(self.api(self.dev).delete(self.url(f"invitations/{inv['id']}/")).status_code, 403)
        self.assertEqual(self.api(self.owner).delete(self.url(f"invitations/{inv['id']}/")).status_code, 204)
        self.assertEqual(self.api(self.outsider).get("/api/invitations/").json(), [])
        self.assertEqual(self.api(self.outsider).post(f"/api/invitations/{inv['id']}/accept/").status_code, 404)

    def test_members_cannot_be_added_directly(self):
        r = self.api(self.owner).post(self.url("members/"), {"identifier": "outsider"}, format="json")
        self.assertEqual(r.status_code, 405)


class ProjectDateRuleTests(TestCase):
    def setUp(self):
        self.c = APIClient()
        self.c.force_authenticate(User.objects.create_user("pm", "pm@example.com", "pw"))

    def create(self, **dates):
        from datetime import date, timedelta
        today = date.today()
        body = {"name": "P", **{k: (today + timedelta(days=v)).isoformat() for k, v in dates.items()}}
        return self.c.post("/api/projects/", body, format="json")

    def test_valid_dates(self):
        self.assertEqual(self.create(start_date=0, end_date=10, release_date=10).status_code, 201)
        self.assertEqual(self.create(start_date=1, end_date=5, release_date=20).status_code, 201)
        self.assertEqual(self.create().status_code, 201)  # dates are optional

    def test_release_must_be_future(self):
        for d in (0, -1):
            r = self.create(release_date=d)
            self.assertEqual(r.status_code, 400)
            self.assertIn("release_date", r.json())

    def test_window_start_today_or_later_and_not_past_end_or_release(self):
        self.assertIn("start_date", self.create(start_date=-1).json())
        self.assertIn("start_date", self.create(start_date=5, end_date=3).json())
        self.assertIn("start_date", self.create(start_date=5, release_date=3).json())

    def test_window_end_future_and_not_past_release(self):
        self.assertIn("end_date", self.create(end_date=0).json())
        self.assertIn("end_date", self.create(end_date=10, release_date=5).json())

    def test_editing_existing_project_allows_past_start(self):
        from datetime import date, timedelta
        p = self.create(start_date=0, end_date=10, release_date=12).json()
        Project.objects.filter(pk=p["id"]).update(start_date=date.today() - timedelta(days=30))
        r = self.c.patch(f"/api/projects/{p['id']}/", {"name": "Renamed"}, format="json")
        self.assertEqual(r.status_code, 200)
        r = self.c.patch(f"/api/projects/{p['id']}/", {"end_date": (date.today() + timedelta(days=20)).isoformat()},
                         format="json")
        self.assertIn("end_date", r.json())  # still can't pass the release date


class WorkbookTests(ProjectTestBase):
    def xlsx(self, wb, name="p.xlsx"):
        import io
        buf = io.BytesIO()
        wb.save(buf)
        return SimpleUploadedFile(name, buf.getvalue())

    def load(self, resp):
        import io
        from openpyxl import load_workbook
        self.assertEqual(resp.status_code, 200)
        return load_workbook(io.BytesIO(resp.content))

    def set_project(self, wb, **values):
        labels = {"name": "Name", "code": "Code", "start_date": "Window start", "end_date": "Window end",
                  "release_date": "Release date"}
        ws = wb["Project"]
        for row in ws.iter_rows(min_row=2):
            for attr, label in labels.items():
                if row[0].value == label and attr in values:
                    row[1].value = values[attr]

    def test_template_creates_new_project(self):
        from datetime import date, timedelta
        c = self.api(self.dev)
        wb = self.load(c.get("/api/projects/template/"))
        self.assertEqual(wb.sheetnames, ["Instructions", "Project", "Sprints", "Milestones", "Tasks", "Risks",
                                         "Issues", "Team"])
        today = date.today()
        self.set_project(wb, name="From Excel", code="FX", start_date=today, end_date=today + timedelta(days=30),
                         release_date=today + timedelta(days=40))
        wb["Sprints"].append([None, "S1", 1, today, today + timedelta(days=14), "Build"])
        wb["Milestones"].append([None, "MVP", today + timedelta(days=20), "S1", "No", ""])
        wb["Tasks"].append([None, "FR-1", "Login", "", "S1", "MVP", "dev", 3, "In progress", 40, "High", None, None])
        wb["Tasks"].append([None, None, "Logout", "", "S2", None, "ghost", 1, None, None, None, None, None])
        wb["Risks"].append([None, "Scope creep", "", "High", "Medium", "Open", "dev", "Freeze scope"])
        wb["Issues"].append([None, "Crash on login", "", "Critical", "Open", None, "FR-1"])

        r = c.post("/api/projects/import/", {"file": self.xlsx(wb)}, format="multipart")
        self.assertEqual(r.status_code, 201, r.content)
        body = r.json()
        self.assertEqual(body["project"]["my_role"], "owner")
        self.assertEqual(body["summary"]["tasks"], {"created": 2, "updated": 0})
        self.assertEqual(body["summary"]["sprints"]["created"], 2)  # S1 + S2 referenced by a task
        self.assertTrue(any("ghost" in w for w in body["warnings"]))

        p = Project.objects.get(pk=body["project"]["id"])
        t = p.tasks.get(code="FR-1")
        self.assertEqual((t.assignee, t.status, t.percent_done, t.priority, t.sprint.name, t.milestone.name),
                         (self.dev, "in_progress", 40, "high", "S1", "MVP"))
        self.assertEqual(p.risks.get().probability, 3)
        self.assertEqual(p.issues.get().task, t)

    def test_export_edit_reimport_updates_project(self):
        c = self.api(self.owner)
        c.post(self.url("tasks/"), {"title": "Old title", "code": "FR-9"}, format="json")
        wb = self.load(c.get(self.url("workbook/")))
        ws = wb["Tasks"]
        self.assertEqual(ws.cell(row=2, column=2).value, "FR-9")
        ws.cell(row=2, column=3).value = "New title"
        ws.cell(row=2, column=9).value = "Done"
        ws.append([None, None, "Brand new", "", None, None, None, 2, None, None, None, None, None])
        self.set_project(wb, name="PES renamed")

        r = c.post(self.url("workbook/"), {"file": self.xlsx(wb)}, format="multipart")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(r.json()["summary"]["tasks"], {"created": 1, "updated": 1})
        t = Task.objects.get(project_id=self.pid, code="FR-9")
        self.assertEqual((t.title, t.status, t.percent_done), ("New title", "done", 100))
        self.assertEqual(Project.objects.get(pk=self.pid).name, "PES renamed")

        # Importing the same file again changes nothing new (no duplicates).
        wb = self.load(c.get(self.url("workbook/")))
        r = c.post(self.url("workbook/"), {"file": self.xlsx(wb)}, format="multipart")
        self.assertEqual(r.json()["summary"]["tasks"], {"created": 0, "updated": 2})
        self.assertEqual(Task.objects.filter(project_id=self.pid).count(), 2)

    def test_errors_import_nothing(self):
        from datetime import date, timedelta
        c = self.api(self.owner)
        wb = self.load(c.get("/api/projects/template/"))
        self.set_project(wb, name="Bad", release_date=date.today() - timedelta(days=1))
        wb["Tasks"].append([None, None, "Task", "", None, None, None, None, None, None, None, None, None])
        r = c.post("/api/projects/import/", {"file": self.xlsx(wb)}, format="multipart")
        self.assertEqual(r.status_code, 400)
        self.assertIn("release date", " ".join(r.json()["errors"]))
        self.assertFalse(Project.objects.filter(name="Bad").exists())

        wb = self.load(c.get(self.url("workbook/")))
        wb["Tasks"].append([None, None, "Should roll back", "", None, None, None, None, None, None, None,
                            "not a date", None])
        r = c.post(self.url("workbook/"), {"file": self.xlsx(wb)}, format="multipart")
        self.assertEqual(r.status_code, 400)
        self.assertFalse(Task.objects.filter(title="Should roll back").exists())

        bad = SimpleUploadedFile("x.xlsx", b"not excel")
        self.assertEqual(c.post("/api/projects/import/", {"file": bad}, format="multipart").status_code, 400)

    def test_permissions(self):
        self.assertEqual(self.api(self.viewer).get(self.url("workbook/")).status_code, 200)
        wb = self.load(self.api(self.viewer).get(self.url("workbook/")))
        r = self.api(self.dev).post(self.url("workbook/"), {"file": self.xlsx(wb)}, format="multipart")
        self.assertEqual(r.status_code, 403)
        self.assertEqual(self.api(self.outsider).get(self.url("workbook/")).status_code, 404)
