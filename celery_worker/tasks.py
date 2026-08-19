from celery import Celery

celery = Celery('tasks', broker='redis://redis:6379/0', backend='redis://redis:6379/0')

@celery.task(name='celery_worker.tasks.example_task')
def example_task():
    return 'Background task completed!'
