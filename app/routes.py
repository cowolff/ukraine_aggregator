from . import app, celery
from flask import jsonify, redirect, url_for, render_template_string

@app.route('/')
def index():
    return 'Flask app is running!'

@app.route('/run-task')
def run_task():
    task = task = celery.send_task('celery_worker.tasks.example_task')
    return redirect(url_for('wait_for_result', task_id=task.id))

@app.route('/wait/<task_id>')
def wait_for_result(task_id):
    result = celery.AsyncResult(task_id)
    if result.ready():
        return render_template_string('<h1>Result: {{ result }}</h1>', result=result.result)
    else:
        return render_template_string('''
            <h1>Waiting for result...</h1>
            <meta http-equiv="refresh" content="2">
        ''')