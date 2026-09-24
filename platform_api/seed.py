"""Deterministic seed data. Run with: uv run python -m platform_api.seed

All names and clients are fictional. There is no randomness: every row below is
hardcoded. Dates are *relative* — time-entry dates are fixed day-offsets from the
Monday of the week in which the seed runs, and task due dates are fixed offsets
from today — so "recent weeks" stays recent whenever you demo, while the shape of
the data is identical on every run. Re-running drops and recreates all tables.
"""

from datetime import date, timedelta

from sqlmodel import Session, SQLModel

from .db import engine as default_engine
from .models import Employee, Project, Task, TimeEntry

# (name, role, department, weekly_capacity_hours)
EMPLOYEES = [
    ("Ava Chen", "Senior Consultant", "Strategy", 40),
    ("Marcus Webb", "Data Engineer", "Technology", 40),
    ("Priya Sharma", "Project Manager", "Delivery", 40),
    ("Diego Ruiz", "Business Analyst", "Strategy", 40),
    ("Tessa Morgan", "UX Designer", "Experience", 32),
    ("Liam O'Connor", "Software Developer", "Technology", 40),
    ("Nora Haddad", "Finance Lead", "Operations", 24),
    ("Felix Grant", "Junior Analyst", "Strategy", 40),
]

# (name, client, status, budget_hours)
PROJECTS = [
    ("Orion Data Migration", "Northwind Logistics", "active", 480),
    ("Atlas KPI Dashboard", "Helios Energy", "active", 320),
    ("Quartz CRM Cleanup", "Bluebird Retail", "on_hold", 160),
    ("Phoenix Onboarding Portal", "Cascade Health", "active", 240),
    ("Legacy Archive Extract", "Sterling Mutual", "closed", 120),
]

# (project_id, assignee_id | None, title, status, due_in_days | None)
# ids are 1-based row numbers from the tables above (insertion order is fixed).
TASKS = [
    (1, 2, "Map legacy warehouse schema", "done", -10),
    (1, 2, "Build extraction pipeline for orders", "in_progress", 4),
    (1, 6, "Write migration validation suite", "in_progress", 6),
    (1, 4, "Document rollback procedure", "todo", 12),
    (1, None, "Plan cutover weekend", "todo", 21),
    (2, 5, "Wireframe executive dashboard", "done", -15),
    (2, 5, "Usability test round 1", "in_progress", 3),
    (2, 2, "Connect dashboard to data mart", "in_progress", 5),
    (2, 8, "Draft KPI definitions with client", "done", -7),
    (2, None, "Set up alerting thresholds", "todo", 14),
    (3, 4, "Audit duplicate account records", "todo", None),
    (3, 8, "Propose deduplication rules", "todo", None),
    (4, 3, "Kickoff workshop agenda", "done", -20),
    (4, 5, "Design onboarding flow screens", "in_progress", 2),
    (4, 6, "Implement SSO stub", "todo", 9),
    (4, 1, "Draft training curriculum", "in_progress", 7),
    (4, 7, "Set up project budget tracking", "done", -3),
    (5, 6, "Export archived contracts", "done", -30),
    (5, 4, "Verify archive completeness", "done", -25),
    (1, 1, "Stakeholder status briefing", "todo", 1),
]

# (employee_id, project_id, week_offset, weekday, hours, note)
# week_offset 0 = the current week, -1 = last week, ...; weekday 0 = Monday.
# The current week only has Monday/Tuesday entries so the demo never depends on
# how far into the week you run it. 60 entries total.
TIME_ENTRIES = [
    # three weeks ago
    (1, 4, -3, 0, 6, "Curriculum outline"),
    (1, 1, -3, 2, 5, "Stakeholder interviews"),
    (1, 4, -3, 4, 4, "Client sync"),
    (2, 1, -3, 0, 8, "Schema mapping"),
    (2, 1, -3, 1, 7, "Schema mapping"),
    (2, 2, -3, 3, 6, "Data mart queries"),
    (3, 4, -3, 1, 6, "Sprint planning"),
    (3, 4, -3, 3, 5, "Risk review"),
    (4, 1, -3, 2, 7, "Rollback doc research"),
    (4, 3, -3, 4, 3, "Duplicate audit prep"),
    (5, 2, -3, 0, 6, "Dashboard wireframes"),
    (5, 4, -3, 2, 5, "Onboarding flow sketches"),
    (6, 1, -3, 1, 8, "Validation suite"),
    (6, 5, -3, 3, 4, "Archive export checks"),
    (7, 4, -3, 0, 5, "Budget setup"),
    (8, 2, -3, 1, 7, "KPI definitions"),
    (8, 3, -3, 4, 4, "Dedup rules draft"),
    # two weeks ago
    (1, 4, -2, 0, 7, "Training curriculum"),
    (1, 2, -2, 3, 4, "KPI workshop"),
    (2, 1, -2, 0, 8, "Extraction pipeline"),
    (2, 1, -2, 2, 8, "Extraction pipeline"),
    (2, 2, -2, 4, 5, "Mart connection"),
    (3, 4, -2, 1, 6, "Status reporting"),
    (3, 1, -2, 3, 4, "Cutover planning"),
    (4, 1, -2, 0, 6, "Rollback procedure"),
    (4, 3, -2, 2, 5, "Account audit"),
    (5, 2, -2, 1, 7, "Usability prep"),
    (5, 4, -2, 3, 6, "Flow screens"),
    (6, 1, -2, 0, 8, "Validation suite"),
    (6, 4, -2, 2, 6, "SSO stub spike"),
    (7, 4, -2, 1, 5, "Budget tracking"),
    (7, 4, -2, 3, 4, "Invoice reconciliation"),
    (8, 2, -2, 0, 6, "Alerting research"),
    (8, 1, -2, 4, 5, "Migration docs support"),
    # last week
    (1, 1, -1, 0, 5, "Stakeholder briefing prep"),
    (1, 4, -1, 2, 6, "Curriculum review"),
    (1, 2, -1, 4, 4, "Dashboard feedback"),
    (2, 1, -1, 0, 8, "Pipeline hardening"),
    (2, 1, -1, 3, 7, "Order data backfill"),
    (3, 4, -1, 0, 6, "Sprint review"),
    (3, 1, -1, 2, 5, "Cutover checklist"),
    (3, 2, -1, 4, 3, "Client demo prep"),
    (4, 1, -1, 1, 6, "Rollback dry run"),
    (4, 3, -1, 3, 4, "Dedup analysis"),
    (5, 2, -1, 1, 8, "Usability test round 1"),
    (5, 4, -1, 3, 5, "Screen revisions"),
    (6, 1, -1, 1, 7, "Validation fixes"),
    (6, 4, -1, 4, 5, "SSO stub"),
    (7, 4, -1, 2, 6, "Budget review"),
    (8, 2, -1, 2, 6, "Threshold config"),
    (8, 1, -1, 4, 4, "Schema docs"),
    # this week (Monday/Tuesday only)
    (1, 4, 0, 0, 5, "Training session 1"),
    (2, 1, 0, 0, 8, "Cutover rehearsal"),
    (3, 4, 0, 1, 6, "Weekly planning"),
    (4, 1, 0, 0, 4, "Rollback doc final"),
    (5, 2, 0, 1, 7, "Dashboard polish"),
    (6, 1, 0, 1, 8, "Validation suite green"),
    (7, 4, 0, 0, 5, "Invoice batch"),
    (8, 3, 0, 1, 5, "Dedup proposal"),
    (1, 2, 0, 1, 3, "KPI sign-off"),
]


def seed(engine=None) -> dict[str, int]:
    """Drop all tables, recreate them, and insert the fixed dataset.

    Returns row counts per table so callers (and tests) can verify the result.
    """
    engine = engine or default_engine
    SQLModel.metadata.drop_all(engine)
    SQLModel.metadata.create_all(engine)

    today = date.today()
    monday = today - timedelta(days=today.weekday())

    with Session(engine) as session:
        for name, role, department, capacity in EMPLOYEES:
            session.add(
                Employee(
                    name=name, role=role, department=department, weekly_capacity_hours=capacity
                )
            )
        for name, client, status, budget in PROJECTS:
            session.add(Project(name=name, client=client, status=status, budget_hours=budget))
        for project_id, assignee_id, title, status, due_in_days in TASKS:
            due = today + timedelta(days=due_in_days) if due_in_days is not None else None
            session.add(
                Task(
                    project_id=project_id,
                    assignee_id=assignee_id,
                    title=title,
                    status=status,
                    due_date=due,
                )
            )
        for employee_id, project_id, week_offset, weekday, hours, note in TIME_ENTRIES:
            session.add(
                TimeEntry(
                    employee_id=employee_id,
                    project_id=project_id,
                    date=monday + timedelta(weeks=week_offset, days=weekday),
                    hours=hours,
                    note=note,
                )
            )
        session.commit()

    return {
        "employees": len(EMPLOYEES),
        "projects": len(PROJECTS),
        "tasks": len(TASKS),
        "time_entries": len(TIME_ENTRIES),
    }


if __name__ == "__main__":
    counts = seed()
    print("Seeded:", ", ".join(f"{n} {table}" for table, n in counts.items()))
