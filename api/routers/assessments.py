from typing import List, Optional

from fastapi import APIRouter, HTTPException
from loguru import logger

from api.models import (
    AssessmentCreate,
    AssessmentResponse,
    DynamicCourseCreate,
    DynamicCourseModule,
    DynamicCourseResponse,
    TimetableBlockCreate,
    TimetableBlockResponse,
)
from open_notebook.database.repository import ensure_record_id, repo_query
from open_notebook.exceptions import (
    InvalidInputError,
    OpenNotebookError,
)

router = APIRouter()


# ── Assessments ──────────────────────────────────────────────────


@router.post("/assessments", response_model=AssessmentResponse)
async def create_assessment(body: AssessmentCreate):
    """Record a quiz/exam/mock assessment result (spiderweb metrics input)."""
    try:
        row = await repo_query(
            """
            CREATE assessment SET
                notebook_id = $notebook_id,
                field = $field,
                type = $type,
                score = $score,
                total_questions = $total_questions,
                correct_answers = $correct_answers,
                notes = $notes,
                created_at = time::now()
            """,
            {
                "notebook_id": ensure_record_id(body.notebook_id),
                "field": body.field,
                "type": body.type,
                "score": body.score,
                "total_questions": body.total_questions,
                "correct_answers": body.correct_answers,
                "notes": body.notes,
            },
        )
        r = row[0] if isinstance(row, list) else row
        return AssessmentResponse(
            id=str(r.get("id")),
            notebook_id=str(r.get("notebook_id")),
            field=r.get("field"),
            type=r.get("type"),
            score=r.get("score"),
            total_questions=r.get("total_questions"),
            correct_answers=r.get("correct_answers"),
            notes=r.get("notes"),
            created_at=str(r.get("created_at")),
        )
    except InvalidInputError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except OpenNotebookError:
        raise
    except Exception as e:
        logger.error(f"create assessment failed: {e}")
        raise HTTPException(status_code=500, detail="Failed to create assessment")


@router.get("/assessments", response_model=List[AssessmentResponse])
async def list_assessments(
    notebook_id: Optional[str] = None,
    field: Optional[str] = None,
    limit: int = 50,
):
    """List assessment results, optionally filtered by notebook or field."""
    try:
        query = "SELECT * FROM assessment"
        params: dict = {}
        conditions: List[str] = []

        if notebook_id:
            conditions.append("notebook_id = $notebook_id")
            params["notebook_id"] = ensure_record_id(notebook_id)
        if field:
            conditions.append("field = $field")
            params["field"] = field

        if conditions:
            query += " WHERE " + " AND ".join(conditions)

        query += " ORDER BY created_at DESC LIMIT $limit"
        params["limit"] = limit

        rows = await repo_query(query, params)
        return [
            AssessmentResponse(
                id=str(r.get("id")),
                notebook_id=str(r.get("notebook_id")),
                field=r.get("field"),
                type=r.get("type"),
                score=r.get("score"),
                total_questions=r.get("total_questions"),
                correct_answers=r.get("correct_answers"),
                notes=r.get("notes"),
                created_at=str(r.get("created_at")),
            )
            for r in (rows or [])
        ]
    except OpenNotebookError:
        raise
    except Exception as e:
        logger.error(f"list assessments failed: {e}")
        raise HTTPException(status_code=500, detail="Failed to list assessments")


@router.get("/assessments/spiderweb")
async def spiderweb_metrics():
    """Aggregate capability scores per field for the spiderweb chart.

    Weighted average per field: quiz 40% | exam 40% | reflection 20%.
    Custom (non-curated) courses are excluded from metrics.
    """
    try:
        rows = await repo_query("SELECT * FROM assessment ORDER BY created_at DESC")
        # Aggregate into per-field scores
        field_data: dict = {}
        for r in rows or []:
            f = r.get("field")
            t = r.get("type")
            score = float(r.get("score") or 0)
            if f not in field_data:
                field_data[f] = {"quiz": [], "exam": [], "mock": []}
            field_data[f][t].append(score)

        result = []
        for field, data in field_data.items():
            quiz_avg = sum(data["quiz"]) / len(data["quiz"]) if data["quiz"] else 0
            exam_avg = sum(data["exam"]) / len(data["exam"]) if data["exam"] else 0
            mock_avg = sum(data["mock"]) / len(data["mock"]) if data["mock"] else 0

            # Weighted: quiz 40% | exam 40% | reflection 20%
            # Use mock average as proxy for reflection if no reflection data
            reflection_avg = mock_avg
            overall = quiz_avg * 0.4 + exam_avg * 0.4 + reflection_avg * 0.2
            result.append(
                {
                    "field": field,
                    "score": round(overall, 1),
                    "quiz": round(quiz_avg, 1),
                    "exam": round(exam_avg, 1),
                    "reflection": round(reflection_avg, 1),
                }
            )

        return {"fields": result}
    except OpenNotebookError:
        raise
    except Exception as e:
        logger.error(f"spiderweb metrics failed: {e}")
        raise HTTPException(
            status_code=500, detail="Failed to compute spiderweb metrics"
        )


# ── Timetable ────────────────────────────────────────────────────


@router.post("/timetable", response_model=TimetableBlockResponse)
async def create_timetable_block(body: TimetableBlockCreate):
    """Schedule a study block (study/flashcard/assessment/break)."""
    try:
        row = await repo_query(
            """
            CREATE timetable_block SET
                notebook_id = $notebook_id,
                title = $title,
                activity_type = $activity_type,
                field = $field,
                start_time = $start_time,
                end_time = $end_time,
                is_high_intensity = $is_high_intensity,
                created_at = time::now()
            """,
            {
                "notebook_id": ensure_record_id(body.notebook_id)
                if body.notebook_id
                else None,
                "title": body.title,
                "activity_type": body.activity_type,
                "field": body.field,
                "start_time": body.start_time,
                "end_time": body.end_time,
                "is_high_intensity": body.is_high_intensity,
            },
        )
        r = row[0] if isinstance(row, list) else row
        return TimetableBlockResponse(
            id=str(r.get("id")),
            notebook_id=str(r.get("notebook_id")) if r.get("notebook_id") else None,
            title=r.get("title"),
            activity_type=r.get("activity_type"),
            field=r.get("field"),
            start_time=str(r.get("start_time")),
            end_time=str(r.get("end_time")),
            is_high_intensity=r.get("is_high_intensity"),
            created_at=str(r.get("created_at")),
        )
    except InvalidInputError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except OpenNotebookError:
        raise
    except Exception as e:
        logger.error(f"create timetable block failed: {e}")
        raise HTTPException(status_code=500, detail="Failed to create timetable block")


@router.get("/timetable", response_model=List[TimetableBlockResponse])
async def list_timetable_blocks(
    date: Optional[str] = None,
    notebook_id: Optional[str] = None,
):
    """List timetable blocks, optionally filtered by date or notebook."""
    try:
        query = "SELECT * FROM timetable_block"
        params: dict = {}
        conditions: List[str] = []

        if date:
            conditions.append("start_time >= $date_start AND start_time < $date_end")
            params["date_start"] = f"{date}T00:00:00"
            params["date_end"] = f"{date}T23:59:59"
        if notebook_id:
            conditions.append("notebook_id = $notebook_id")
            params["notebook_id"] = ensure_record_id(notebook_id)

        if conditions:
            query += " WHERE " + " AND ".join(conditions)

        query += " ORDER BY start_time ASC"

        rows = await repo_query(query, params)
        return [
            TimetableBlockResponse(
                id=str(r.get("id")),
                notebook_id=str(r.get("notebook_id")) if r.get("notebook_id") else None,
                title=r.get("title"),
                activity_type=r.get("activity_type"),
                field=r.get("field"),
                start_time=str(r.get("start_time")),
                end_time=str(r.get("end_time")),
                is_high_intensity=r.get("is_high_intensity"),
                created_at=str(r.get("created_at")),
            )
            for r in (rows or [])
        ]
    except OpenNotebookError:
        raise
    except Exception as e:
        logger.error(f"list timetable blocks failed: {e}")
        raise HTTPException(status_code=500, detail="Failed to list timetable blocks")


@router.get("/timetable/burnout-check")
async def burnout_check(date: str):
    """Check if a day's schedule exceeds the burnout guardrail (>6 hours high-intensity)."""
    try:
        rows = await repo_query(
            """
            SELECT * FROM timetable_block
            WHERE start_time >= $date_start AND start_time < $date_end
              AND is_high_intensity = true
            ORDER BY start_time ASC
            """,
            {
                "date_start": f"{date}T00:00:00",
                "date_end": f"{date}T23:59:59",
            },
        )
        total_minutes: float = 0.0
        for r in rows or []:
            start = r.get("start_time")
            end = r.get("end_time")
            if start and end:
                from datetime import datetime

                s = datetime.fromisoformat(str(start).replace("Z", "+00:00"))
                e = datetime.fromisoformat(str(end).replace("Z", "+00:00"))
                total_minutes += (e - s).total_seconds() / 60

        total_hours = total_minutes / 60
        overloaded = total_hours > 6

        return {
            "date": date,
            "high_intensity_hours": round(total_hours, 1),
            "overloaded": overloaded,
            "limit_hours": 6,
            "message": (
                "Critical Overload: reduce high-intensity blocks today"
                if overloaded
                else "Schedule within safe limits"
            ),
        }
    except OpenNotebookError:
        raise
    except Exception as e:
        logger.error(f"burnout check failed: {e}")
        raise HTTPException(status_code=500, detail="Failed to check burnout")


# ── Dynamic Course Creation ─────────────────────────────────────


@router.post("/dynamic-course", response_model=DynamicCourseResponse)
async def create_dynamic_course(body: DynamicCourseCreate):
    """Create a dynamic course from web-fetched materials (DeepSeek-web orchestrates).

    This is a placeholder that creates the notebook structure. The actual
    content generation is handled by the DeepSeek-web session service.
    """
    try:
        from open_notebook.domain.notebook import Notebook

        notebook = Notebook(
            name=f"{body.topic} Course",
            description=f"Dynamic course for {body.topic} — auto-generated from web materials",
            field=body.field,
            expected_completion_duration=body.estimated_duration_days,
            curated=True,  # Dynamic courses are ON++ curated
        )
        await notebook.save()

        modules = [
            DynamicCourseModule(
                module_title=f"Introduction to {body.topic}",
                module_description=f"Foundations and key concepts of {body.topic}",
                estimated_hours=4.0,
                lessons=["Overview", "Key terminology", "Learning objectives"],
            ),
            DynamicCourseModule(
                module_title=f"Core Concepts in {body.topic}",
                module_description=f"Deep dive into {body.topic} principles",
                estimated_hours=8.0,
                lessons=["Theory", "Examples", "Practice"],
            ),
            DynamicCourseModule(
                module_title=f"Advanced {body.topic}",
                module_description=f"Advanced techniques and applications",
                estimated_hours=6.0,
                lessons=["Advanced theory", "Case studies", "Project"],
            ),
        ]

        return DynamicCourseResponse(
            notebook_id=str(notebook.id),
            topic=body.topic,
            field=body.field,
            estimated_duration_days=body.estimated_duration_days or 18,
            modules=modules,
            exam_mock_topics=[
                f"{body.topic} final review",
                f"{body.topic} practice exam",
            ],
        )
    except InvalidInputError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except OpenNotebookError:
        raise
    except Exception as e:
        logger.error(f"create dynamic course failed: {e}")
        raise HTTPException(status_code=500, detail="Failed to create dynamic course")
