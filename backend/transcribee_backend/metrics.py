import datetime
import secrets
from abc import abstractmethod
from typing import List

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from prometheus_client import Gauge
from sqlmodel import Session, col, func, select
from transcribee_proto.api import TaskType

from transcribee_backend.config import settings
from transcribee_backend.db import SessionContextManager
from transcribee_backend.helpers.time import now_tz_aware
from transcribee_backend.models.document import Document
from transcribee_backend.models.task import Task, TaskAttempt, TaskState
from transcribee_backend.models.user import User
from transcribee_backend.models.worker import Worker


class Metric:
    @abstractmethod
    def refresh(self, session: Session):
        pass


class TasksInState(Metric):
    def __init__(self):
        self.collector = Gauge(
            "transcribee_tasks", "Number of tasks", ["state", "task_type"]
        )

    def refresh(self, session: Session):
        result = session.exec(
            select(Task.state, Task.task_type, func.count()).group_by(
                Task.state, Task.task_type
            )
        ).all()
        counts = {(x, y): 0 for x in TaskState for y in TaskType}
        for state, task_type, count in result:
            counts[(state, task_type)] = count
        for (state, task_type), count in counts.items():
            self.collector.labels(state=state.value, task_type=task_type.value).set(
                count
            )


class Workers(Metric):
    def __init__(self):
        self.collector = Gauge("transcribee_workers", "Workers", ["group"])

    def refresh(self, session: Session):
        result = session.exec(
            select(func.count(Worker.id)).where(col(Worker.deactivated_at).is_(None))
        ).one()
        self.collector.labels(group="all").set(result)

        now = now_tz_aware()
        worker_timeout_ago = now - datetime.timedelta(seconds=settings.worker_timeout)
        result = session.exec(
            select(func.count(Worker.id)).where(
                col(Worker.last_seen) >= worker_timeout_ago,
            )
        ).one()
        self.collector.labels(group="alive").set(result)


class Users(Metric):
    def __init__(self):
        self.collector = Gauge("transcribee_users", "Registered users")

    def refresh(self, session: Session):
        result = session.exec(select(func.count(User.id))).one()
        self.collector.set(result)


class Documents(Metric):
    def __init__(self):
        self.collector = Gauge("transcribe_documents", "Documents")

    def refresh(self, session: Session):
        result = session.exec(select(func.count(Document.id))).one()
        self.collector.set(result)


class Queue(Metric):
    def __init__(self):
        self.collector = Gauge(
            "transcribee_queue_seconds", "Queue length in seconds", ["task_type"]
        )

    def refresh(self, session: Session):
        result = session.exec(
            select(
                Task.task_type,
                func.coalesce(
                    func.sum(
                        Document.duration * (1 - func.coalesce(TaskAttempt.progress, 0))
                    ),
                    0,
                ),
            )
            .join(Task, Task.document_id == Document.id)
            .join(TaskAttempt, Task.current_attempt_id == TaskAttempt.id, isouter=True)
            .group_by(Task.task_type)
            .where(col(Task.state).in_(["NEW", "ASSIGNED"]))
        ).all()
        counts = {x: 0 for x in TaskType}
        for task_type, count in result:
            counts[task_type] = count
        for task_type, count in counts.items():
            self.collector.labels(task_type=task_type.value).set(count)


METRIC_CLASSES: List[type[Metric]] = [TasksInState, Workers, Users, Documents, Queue]
METRICS: List[Metric] = []


def refresh_metrics():
    with SessionContextManager(path="repeating_task:refresh_metrics") as session:
        for metric in METRICS:
            metric.refresh(session)


def init_metrics():
    for klass in METRIC_CLASSES:
        METRICS.append(klass())


security = HTTPBasic()


def metrics_auth(credentials: HTTPBasicCredentials = Depends(security)):
    is_correct_username = secrets.compare_digest(
        credentials.username.encode("utf8"), settings.metrics_username.encode("utf8")
    )
    is_correct_password = secrets.compare_digest(
        credentials.password.encode("utf8"), settings.metrics_password.encode("utf8")
    )
    if not (is_correct_username and is_correct_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            headers={"WWW-Authenticate": "Basic"},
        )
    return credentials.username
