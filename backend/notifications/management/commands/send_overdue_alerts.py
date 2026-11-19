"""Create overdue-task alerts for everyone.

The app already checks overdue tasks whenever a user opens their notifications, so this
is optional. Schedule it (e.g. cron: `0 7 * * * .venv/bin/python manage.py send_overdue_alerts`)
if you want alerts waiting even for people who haven't opened the app. Safe to run repeatedly.
"""

from django.core.management.base import BaseCommand

from notifications.service import check_overdue


class Command(BaseCommand):
    help = "Create one 'overdue' notification per overdue task for its owner (idempotent)."

    def handle(self, *args, **options):
        n = check_overdue()
        self.stdout.write(self.style.SUCCESS(f"Checked overdue tasks ({n} alert rows processed)."))
