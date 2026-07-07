"""
ai.py
-------------------------------------------------
Gemini-powered intelligence for StudySync.

Uses the new `google.genai` SDK (NOT the deprecated
`google.generativeai` package).

Every function degrades gracefully: if GEMINI_API_KEY is
missing or the API call fails, a sensible rule-based
fallback is returned instead of crashing the app, so the
rest of StudySync keeps working even before a key is added.
-------------------------------------------------
"""

import os
import json
from google import genai

MODEL_NAME = "gemini-2.5-flash"

_client = None


def _get_client():
    """Lazily create the genai client so a missing key doesn't crash import."""
    global _client
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        return None
    if _client is None:
        _client = genai.Client(api_key=api_key)
    return _client


def _call_gemini(prompt, json_mode=False):
    """
    Low-level helper: sends a prompt to Gemini and returns text.
    Returns None on any failure so callers can fall back gracefully.
    """
    client = _get_client()
    if client is None:
        return None
    try:
        config = {"response_mime_type": "application/json"} if json_mode else {}
        response = client.models.generate_content(
            model=MODEL_NAME,
            contents=prompt,
            config=config,
        )
        return response.text
    except Exception as e:
        print(f"[ai.py] Gemini call failed: {e}")
        return None


def _context_block(tasks, schedule_today, free_slots):
    """Builds a compact textual context block reused across prompts."""
    return f"""
Pending tasks (title | subject | type | due_date | due_time | estimated_hours | priority | progress%):
{json.dumps(tasks, indent=2)}

Today's schedule (title | start_time | end_time):
{json.dumps(schedule_today, indent=2)}

Free time slots today (start_time - end_time):
{json.dumps(free_slots, indent=2)}
"""


def generate_daily_study_plan(tasks, schedule_today, free_slots):
    """
    Ask Gemini to turn free slots + pending tasks into a concrete
    today's study plan. Returns a list of {time, activity, reason} dicts.
    """
    prompt = f"""
You are an academic planning assistant. Using the data below, build a realistic
study plan for TODAY only, fitting study sessions into the free time slots.
Prioritize urgent/high priority tasks and tasks due soonest. Do not schedule
more than the free time available.

{_context_block(tasks, schedule_today, free_slots)}

Respond ONLY with a JSON array, no other text, in this exact shape:
[
  {{"time": "16:00-17:00", "activity": "Physics Assignment - Problem Set 3", "reason": "Due tomorrow, highest priority"}}
]
"""
    text = _call_gemini(prompt, json_mode=True)
    if text:
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass

    # ---- Fallback: simple greedy scheduler if Gemini is unavailable ----
    plan = []
    sorted_tasks = sorted(
        tasks, key=lambda t: (t.get("priority_rank", 2), t.get("due_date") or "9999")
    )
    slot_i = 0
    for task in sorted_tasks:
        if slot_i >= len(free_slots):
            break
        slot = free_slots[slot_i]
        plan.append({
            "time": f"{slot['start_time']}-{slot['end_time']}",
            "activity": f"{task.get('subject', '')} - {task['title']}".strip(" -"),
            "reason": f"Priority {task.get('priority', 'Medium')}, due {task.get('due_date', 'soon')}"
        })
        slot_i += 1
    return plan


def generate_weekly_plan(tasks, schedule_week, free_slots_by_day):
    """Ask Gemini for a 7-day study plan overview."""
    prompt = f"""
You are an academic planning assistant. Build a 7-day (Monday-Sunday) study plan
that distributes the pending tasks below across the available free time slots
per day, prioritizing by due date and priority. Keep daily study load realistic
(don't overload any single day if tasks can be spread out).

Pending tasks:
{json.dumps(tasks, indent=2)}

Weekly schedule (existing classes/events):
{json.dumps(schedule_week, indent=2)}

Free slots per day:
{json.dumps(free_slots_by_day, indent=2)}

Respond ONLY with JSON in this exact shape:
{{
  "Monday": [{{"time": "18:00-19:00", "activity": "..."}}],
  "Tuesday": [],
  "Wednesday": [],
  "Thursday": [],
  "Friday": [],
  "Saturday": [],
  "Sunday": []
}}
"""
    text = _call_gemini(prompt, json_mode=True)
    if text:
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass

    # Fallback: empty skeleton
    return {d: [] for d in
            ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]}


def generate_tomorrow_preview(schedule_tomorrow, tasks_due_tomorrow, wakeup_time):
    """Short evening preview of tomorrow."""
    prompt = f"""
Write a short, friendly evening preview (3-5 sentences, plain text, no markdown)
summarizing tomorrow for a student: their classes, tasks due, and a suggested
bedtime given their wakeup time of {wakeup_time}.

Classes tomorrow: {json.dumps(schedule_tomorrow)}
Tasks due tomorrow: {json.dumps(tasks_due_tomorrow)}
"""
    text = _call_gemini(prompt)
    if text:
        return text.strip()

    # Fallback
    count_classes = len(schedule_tomorrow)
    count_tasks = len(tasks_due_tomorrow)
    return (
        f"Tomorrow you have {count_classes} class(es) and {count_tasks} task(s) due. "
        f"Wake up at {wakeup_time} to stay on track. Aim for 7-8 hours of sleep tonight."
    )


def generate_productivity_tip(stats):
    """One short actionable productivity tip based on recent stats."""
    prompt = f"""
Give ONE short, specific, encouraging productivity tip (max 2 sentences, plain text)
for a student based on these stats: {json.dumps(stats)}
"""
    text = _call_gemini(prompt)
    if text:
        return text.strip()
    return "Try tackling your highest-priority task in your next free slot — small consistent progress beats last-minute cramming."


def break_task_into_steps(task):
    """Break a single assignment/task into concrete sub-steps."""
    prompt = f"""
Break the following academic task into 4-6 concrete, actionable sub-steps a
student can check off. Keep each step short.

Task: {json.dumps(task)}

Respond ONLY with a JSON array of strings, e.g. ["Research topic", "Draft outline"]
"""
    text = _call_gemini(prompt, json_mode=True)
    if text:
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass
    return [
        "Review requirements and rubric",
        "Gather resources / research",
        "Create an outline or draft",
        "Complete first full draft",
        "Review and revise",
        "Final proofread and submit",
    ]


def recommend_study_schedule(task, free_slots):
    """Suggest which free slots to use for a single task given its estimated hours."""
    prompt = f"""
A student needs {task.get('estimated_hours', 1)} hours to complete "{task.get('title')}".
Given these free time slots today/this week: {json.dumps(free_slots)},
recommend which slot(s) to use. Respond ONLY with a JSON array of slot strings
like ["Monday 16:00-17:00", "Tuesday 18:00-19:00"].
"""
    text = _call_gemini(prompt, json_mode=True)
    if text:
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass
    return [f"{s.get('day', '')} {s['start_time']}-{s['end_time']}".strip() for s in free_slots[:2]]


def summarize_daily_progress(completed_tasks, study_minutes, attendance_note):
    """End-of-day summary in a warm, encouraging tone."""
    prompt = f"""
Write a short (2-4 sentence) end-of-day summary for a student, warm and encouraging,
plain text no markdown. They completed {completed_tasks} task(s), studied for
{study_minutes} minutes, and: {attendance_note}
"""
    text = _call_gemini(prompt)
    if text:
        return text.strip()
    return f"Nice work today — you completed {completed_tasks} task(s) and studied for {study_minutes} minutes. Keep the momentum going tomorrow."


def generate_encouraging_message(context):
    """Short motivational one-liner for the dashboard greeting."""
    prompt = f"""
Write ONE short motivational sentence (max 15 words, plain text) for a student's
dashboard greeting. Context: {json.dumps(context)}
"""
    text = _call_gemini(prompt)
    if text:
        return text.strip().strip('"')
    return "One focused hour today beats an anxious week later. You've got this."
