"""
Phase 6A — Authentication routes.

POST /api/auth/register   — Create a new user account
POST /api/auth/login      — Authenticate and receive a JWT token
GET  /api/auth/me         — Get the currently authenticated user's info

All routes return structured JSON responses with clear error messages.
Login errors are intentionally generic to prevent username enumeration.
"""

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.models import User
from app.schemas.auth import (
    LoginRequest,
    RegisterRequest,
    TokenResponse,
    UserResponse,
)
from app.services.auth_service import (
    create_access_token,
    get_current_user,
    hash_password,
    verify_password,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/auth", tags=["auth"])


# ── POST /api/auth/register ────────────────────────────────


@router.post("/register", response_model=UserResponse, status_code=201)
def register_user(payload: RegisterRequest, db: Session = Depends(get_db)):
    """
    Register a new user account.

    Validates that the username and email are unique, hashes the password
    with bcrypt, and returns the new user's public info (no password).

    The user must log in separately to receive a JWT token.
    """
    # Check username uniqueness
    existing_username = (
        db.query(User).filter(User.username == payload.username).first()
    )
    if existing_username:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Username already taken",
        )

    # Check email uniqueness
    if payload.email:
        existing_email = (
            db.query(User).filter(User.email == payload.email).first()
        )
        if existing_email:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Email already registered",
            )

    # Hash the password
    hashed = hash_password(payload.password)

    # Create user
    user = User(
        username=payload.username,
        email=payload.email,
        hashed_password=hashed,
    )
    db.add(user)
    db.commit()
    db.refresh(user)

    logger.info("New user registered: id=%d username=%s", user.id, user.username)
    return user


# ── POST /api/auth/login ───────────────────────────────────


@router.post("/login", response_model=TokenResponse)
def login_user(payload: LoginRequest, db: Session = Depends(get_db)):
    """
    Authenticate a user and return a JWT access token.

    Returns a generic 'Invalid credentials' error for both unknown usernames
    and wrong passwords to prevent username enumeration attacks.

    The returned token should be sent as:
      Authorization: Bearer <token>
    """
    # Find user by username (or email)
    user: Optional[User] = (
        db.query(User).filter(User.username == payload.username).first()
    )

    # Generic error to prevent username enumeration
    if user is None or not user.hashed_password:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials",
            headers={"WWW-Authenticate": "Bearer"},
        )

    if not verify_password(payload.password, user.hashed_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # Create JWT with user ID as subject (must be string per JWT spec)
    access_token = create_access_token(data={"sub": str(user.id)})

    logger.info("User logged in: id=%d username=%s", user.id, user.username)
    return TokenResponse(access_token=access_token)


# ── GET /api/auth/me ───────────────────────────────────────


@router.get("/me", response_model=UserResponse)
def get_me(current_user: User = Depends(get_current_user)):
    """
    Get the currently authenticated user's information.

    Requires a valid Bearer token in the Authorization header.
    Returns the user's public profile (id, username, email, created_at).
    """
    return current_user
