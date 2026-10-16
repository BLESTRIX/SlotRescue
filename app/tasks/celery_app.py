from celery import Celery

from app.core.config import settings

celery = Celery("slotrescue", broker=settings().redis_url)
celery.conf.update(
    broker_connection_retry_on_startup=True,
    task_ignore_result=True,
    task_acks_late=True,
    worker_prefetch_multiplier=1,
    worker_redirect_stdouts=False,
    task_reject_on_worker_lost=True,
    beat_schedule={"discover-durable-jobs": {"task": "slotrescue.tick", "schedule": 5.0}},
    timezone="UTC",
)


@celery.task(name="slotrescue.tick", soft_time_limit=80, time_limit=90)
def tick():
    from app.services.jobs import drain

    return drain(limit=5)
