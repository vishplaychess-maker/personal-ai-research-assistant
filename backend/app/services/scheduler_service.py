"""
Scheduler service for managing scheduled autonomous tasks.

Uses APScheduler to run LangGraph workflows on a cron schedule.
"""
import logging
from datetime import datetime
from typing import Optional

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.models.models import ScheduledTask, ResearchSession, Message, MessageRole
from app.services.langgraph_workflow import run_research_workflow

logger = logging.getLogger(__name__)

# Global scheduler instance
scheduler: Optional[AsyncIOScheduler] = None


def get_scheduler() -> Optional[AsyncIOScheduler]:
    """Get the global scheduler instance."""
    return scheduler


def init_scheduler() -> AsyncIOScheduler:
    """Initialize the APScheduler."""
    global scheduler
    if scheduler is None:
        scheduler = AsyncIOScheduler()
        logger.info("APScheduler initialized")
    return scheduler


def start_scheduler() -> None:
    """Start the scheduler and load all active tasks."""
    global scheduler
    if scheduler is None:
        init_scheduler()
    
    if not scheduler.running:
        scheduler.start()
        logger.info("Scheduler started")
    
    # Load all active tasks from database
    load_scheduled_tasks()


def shutdown_scheduler() -> None:
    """Shutdown the scheduler."""
    global scheduler
    if scheduler and scheduler.running:
        scheduler.shutdown()
        logger.info("Scheduler shutdown")


def load_scheduled_tasks() -> None:
    """Load all active scheduled tasks from database and add to scheduler."""
    global scheduler
    if scheduler is None:
        return
    
    db = SessionLocal()
    try:
        tasks = db.query(ScheduledTask).filter(ScheduledTask.is_active == True).all()
        for task in tasks:
            add_task_to_scheduler(task)
        logger.info(f"Loaded {len(tasks)} active scheduled tasks")
    except Exception as e:
        logger.error(f"Error loading scheduled tasks: {e}")
    finally:
        db.close()


def add_task_to_scheduler(task: ScheduledTask) -> None:
    """Add a single task to the scheduler."""
    global scheduler
    if scheduler is None:
        return
    
    try:
        trigger = CronTrigger.from_crontab(task.cron_expression)
        job_id = f"task_{task.id}"
        
        # Remove existing job if it exists
        if scheduler.get_job(job_id):
            scheduler.remove_job(job_id)
        
        scheduler.add_job(
            execute_scheduled_task,
            trigger=trigger,
            id=job_id,
            args=[task.id],
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
        
        # Update next_run_at in database
        db = SessionLocal()
        try:
            job = scheduler.get_job(job_id)
            if job and job.next_run_time:
                task.next_run_at = job.next_run_time
                db.commit()
        finally:
            db.close()
            
        logger.info(f"Added scheduled task {task.id} with cron '{task.cron_expression}'")
    except Exception as e:
        logger.error(f"Error adding task {task.id} to scheduler: {e}")


def remove_task_from_scheduler(task_id: int) -> None:
    """Remove a task from the scheduler."""
    global scheduler
    if scheduler is None:
        return
    
    job_id = f"task_{task_id}"
    try:
        if scheduler.get_job(job_id):
            scheduler.remove_job(job_id)
            logger.info(f"Removed scheduled task {task_id} from scheduler")
    except Exception as e:
        logger.error(f"Error removing task {task_id} from scheduler: {e}")


def execute_scheduled_task(task_id: int) -> None:
    """
    Execute a scheduled task by running the LangGraph workflow.
    This runs in a separate database session.
    """
    db = SessionLocal()
    try:
        task = db.query(ScheduledTask).filter(ScheduledTask.id == task_id).first()
        if not task or not task.is_active:
            logger.warning(f"Scheduled task {task_id} not found or inactive")
            return
        
        # Verify session exists and belongs to user
        session = db.query(ResearchSession).filter(
            ResearchSession.id == task.session_id,
            ResearchSession.user_id == task.user_id
        ).first()
        
        if not session:
            logger.error(f"Session {task.session_id} not found for task {task_id}")
            return
        
        logger.info(f"Executing scheduled task {task_id}: {task.prompt[:50]}...")
        
        # Run the workflow
        result = run_research_workflow(
            session_id=task.session_id,
            user_input=task.prompt,
            db=db,
            user_id=task.user_id,
        )
        
        # Update task last_run_at
        task.last_run_at = datetime.utcnow()
        
        # Update next_run_at from scheduler
        job = scheduler.get_job(f"task_{task_id}") if scheduler else None
        if job and job.next_run_time:
            task.next_run_at = job.next_run_time
        
        db.commit()
        
        if result.get("error"):
            logger.error(f"Scheduled task {task_id} error: {result['error']}")
        else:
            logger.info(f"Scheduled task {task_id} completed successfully")
            
    except Exception as e:
        logger.error(f"Error executing scheduled task {task_id}: {e}")
        db.rollback()
    finally:
        db.close()


# For testing - run a task immediately
def run_task_now(task_id: int) -> dict:
    """
    Run a scheduled task immediately (for testing/manual trigger).
    Returns the result of the workflow execution.
    """
    db = SessionLocal()
    try:
        task = db.query(ScheduledTask).filter(ScheduledTask.id == task_id).first()
        if not task:
            return {"error": "Task not found"}
        
        session = db.query(ResearchSession).filter(
            ResearchSession.id == task.session_id,
            ResearchSession.user_id == task.user_id
        ).first()
        
        if not session:
            return {"error": "Session not found"}
        
        result = run_research_workflow(
            session_id=task.session_id,
            user_input=task.prompt,
            db=db,
            user_id=task.user_id,
        )
        
        task.last_run_at = datetime.utcnow()
        db.commit()
        
        return result
    except Exception as e:
        logger.error(f"Error running task {task_id} now: {e}")
        db.rollback()
        return {"error": str(e)}
    finally:
        db.close()