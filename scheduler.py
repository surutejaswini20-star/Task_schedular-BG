from datetime import date, timedelta

PRIORITY_WEIGHT = {"High": 3, "Medium": 2, "Low": 1}

def build_schedule(tasks, start_date: date, daily_minutes: int = 180):
    """Greedy study planner: earlier deadlines and higher priorities are scheduled first."""
    ordered = sorted(tasks, key=lambda t: (
        t.due_date,
        -PRIORITY_WEIGHT.get(t.priority, 2),
        -t.duration
    ))
    plan = []
    day = start_date
    used = 0
    for task in ordered:
        duration = max(5, int(task.duration))
        if used + duration > daily_minutes:
            day += timedelta(days=1)
            used = 0
        plan.append({
            "id": task.id,
            "title": task.title,
            "subject": task.subject,
            "due_date": task.due_date,
            "due_time": getattr(task, "due_time", None),
            "study_date": day,
            "duration": duration,
            "priority": task.priority,
            "days_left": (task.due_date - start_date).days,
        })
        used += duration
    return plan
