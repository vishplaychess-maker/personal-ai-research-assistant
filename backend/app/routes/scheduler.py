from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.models import ScheduledTask, ResearchSession, User
from app.routes.auth import get_current_user
from app.services.scheduler_service import (
    add_task_to_scheduler,
    remove_task_from_scheduler,
    run_task_now,
)

router = APIRouter(prefix="/api/scheduler", tags=["scheduler"])


# ── Pydantic schemas ─────────────────────────────────────────


class ScheduledTaskCreate(BaseModel):
    """Create a new scheduled task."""
    session_id: int = Field(..., description="Session ID where results will be saved")
    prompt: str = Field(..., min_length=1, max_length=10000, description="What the AI should do")
    cron_expression: str = Field(..., description="Cron expression (e.g., '0 8 * * *' for 8 AM daily)")


class ScheduledTaskUpdate(BaseModel):
    """Update a scheduled task."""
    prompt: Optional[str] = Field(None, min_length=1, max_length=10000)
    cron_expression: Optional[str] = None
    is_active: Optional[bool] = None


class ScheduledTaskResponse(BaseModel):
    """Response model for scheduled task."""
    id: int
    user_id: int
    session_id: int
    prompt: str
    cron_expression: str
    is_active: bool
    created_at: datetime
    updated_at: datetime
    last_run_at: Optional[datetime]
    next_run_at: Optional[datetime]

    model_config = {"from_attributes": True}


class ScheduledTaskWithSession(ScheduledTaskResponse):
    """Scheduled task with session info."""
    session_title: str


# ── API Routes ───────────────────────────────────────────────


@router.post("", response_model=ScheduledTaskResponse, status_code=201)
def create_scheduled_task(
    payload: ScheduledTaskCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Create a new scheduled task."""
    # Verify session exists and belongs to user
    session = db.query(ResearchSession).filter(
        ResearchSession.id == payload.session_id,
        ResearchSession.user_id == current_user.id
    ).first()
    
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    
    # Validate cron expression
    try:
        from apscheduler.triggers.cron import CronTrigger
        CronTrigger.from_crontab(payload.cron_expression)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Invalid cron expression: {e}")
    
    task = ScheduledTask(
        user_id=current_user.id,
        session_id=payload.session_id,
        prompt=payload.prompt,
        cron_expression=payload.cron_expression,
        is_active=True,
    )
    
    db.add(task)
    db.commit()
    db.refresh(task)
    
    # Add to scheduler
    from app.services.scheduler_service import add_task_to_scheduler
    add_task_to_scheduler(task)
    
    return task


@router.get("", response_model=List[ScheduledTaskWithSession])
def list_scheduled_tasks(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """List all scheduled tasks for the current user."""
    tasks = db.query(ScheduledTask).filter(
        ScheduledTask.user_id == current_user.id
    ).order_by(ScheduledTask.created_at.desc()).all()
    
    result = []
    for task in tasks:
        result.append(ScheduledTaskWithSession(
            id=task.id,
            user_id=task.user_id,
            session_id=task.session_id,
            prompt=task.prompt,
            cron_expression=task.cron_expression,
            is_active=task.is_active,
            created_at=task.created_at,
            updated_at=task.updated_at,
            last_run_at=task.last_run_at,
            next_run_at=task.next_run_at,
            session_title=task.session.title if task.session else "Unknown",
        ))
    
    return result


@router.get("/{task_id}", response_model=ScheduledTaskWithSession)
def get_scheduled_task(
    task_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Get a specific scheduled task."""
    task = db.query(ScheduledTask).filter(
        ScheduledTask.id == task_id,
        ScheduledTask.user_id == current_user.id
    ).first()
    
    if not task:
        raise HTTPException(status_code=404, detail="Scheduled task not found")
    
    return ScheduledTaskWithSession(
        id=task.id,
        user_id=task.user_id,
        session_id=task.session_id,
        prompt=task.prompt,
        cron_expression=task.cron_expression,
        is_active=task.is_active,
        created_at=task.created_at,
        updated_at=task.updated_at,
        last_run_at=task.last_run_at,
        next_run_at=task.next_run_at,
        session_title=task.session.title if task.session else "Unknown",
    )


@router.patch("/{task_id}", response_model=ScheduledTaskResponse)
def update_scheduled_task(
    task_id: int,
    payload: ScheduledTaskUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Update a scheduled task."""
    task = db.query(ScheduledTask).filter(
        ScheduledTask.id == task_id,
        ScheduledTask.user_id == current_user.id
    ).first()
    
    if not task:
        raise HTTPException(status_code=404, detail="Scheduled task not found")
    
    # Validate cron expression if provided
    if payload.cron_expression is not None:
        try:
            from apscheduler.triggers.cron import CronTrigger
            CronTrigger.from_crontab(payload.cron_expression)
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"Invalid cron expression: {e}")
    
    # Update fields
    if payload.prompt is not None:
        task.prompt = payload.prompt
    if payload.cron_expression is not None:
        task.cron_expression = payload.cron_expression
    if payload.is_active is not None:
        task.is_active = payload.is_active
    
    task.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(task)
    
    # Update scheduler
    from app.services.scheduler_service import add_task_to_scheduler, remove_task_from_scheduler
    
    if task.is_active:
        add_task_to_scheduler(task)
    else:
        remove_task_from_scheduler(task.id)
    
    return task


@router.delete("/{task_id}", status_code=204)
def delete_scheduled_task(
    task_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Delete a scheduled task."""
    task = db.query(ScheduledTask).filter(
        ScheduledTask.id == task_id,
        ScheduledTask.user_id == current_user.id
    ).first()
    
    if not task:
        raise HTTPException(status_code=404, detail="Scheduled task not found")
    
    # Remove from scheduler
    from app.services.scheduler_service import remove_task_from_scheduler
    remove_task_from_scheduler(task.id)
    
    # Delete from database
    db.delete(task)
    db.commit()


@router.post("/{task_id}/run", response_model=dict)
def run_scheduled_task_now(
    task_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Run a scheduled task immediately (for testing)."""
    task = db.query(ScheduledTask).filter(
        ScheduledTask.id == task_id,
        ScheduledTask.user_id == current_user.id
    ).first()
    
    if not task:
        raise HTTPException(status_code=404, detail="Scheduled task not found")
    
    result = run_task_now(task_id)
    return result


@router.get("/health", response_model=dict)
def scheduler_health():
    """Check if scheduler is running."""
    from app.services.scheduler_service import get_scheduler
    scheduler = get_scheduler()
    return {
        "running": scheduler.running if scheduler else False,
        "jobs_count": len(scheduler.get_jobs()) if scheduler else 0,
    }