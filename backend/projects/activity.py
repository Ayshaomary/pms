from datetime import date, datetime

from django.db import models

from .models import Activity


def _plain(value):
    if isinstance(value, models.Model):
        return str(value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return value


def snapshot(instance, fields):
    return {f: _plain(getattr(instance, f)) for f in fields}


def diff(before, after):
    return {k: [before[k], after[k]] for k in before if before[k] != after.get(k)}


def log(project, actor, verb, instance=None, entity_type=None, label=None, changes=None):
    Activity.objects.create(
        project=project,
        actor=actor if actor and actor.is_authenticated else None,
        verb=verb,
        entity_type=entity_type or (instance._meta.model_name if instance is not None else "project"),
        entity_id=getattr(instance, "pk", None),
        entity_label=(label or str(instance or ""))[:255],
        changes=changes or {},
    )
