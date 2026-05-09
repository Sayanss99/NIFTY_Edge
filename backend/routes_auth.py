"""Auth API routes — signup, login."""

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session
from pydantic import BaseModel, EmailStr
from database import get_db
from models import User
from auth import hash_password, verify_password, create_access_token, get_current_user

router = APIRouter(prefix="/api/auth", tags=["auth"])


class SignupRequest(BaseModel):
    email: str
    name: str
    password: str


class LoginRequest(BaseModel):
    email: str
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: dict


class UserResponse(BaseModel):
    id: int
    email: str
    name: str


@router.post("/signup", response_model=TokenResponse)
def signup(req: SignupRequest, db: Session = Depends(get_db)):
    existing = db.query(User).filter(User.email == req.email).first()
    if existing:
        raise HTTPException(status_code=400, detail="Email already registered")

    user = User(
        email=req.email,
        name=req.name,
        password_hash=hash_password(req.password),
    )
    db.add(user)
    db.commit()
    db.refresh(user)

    token = create_access_token(user.id)
    return TokenResponse(
        access_token=token,
        user={"id": user.id, "email": user.email, "name": user.name},
    )


@router.post("/login", response_model=TokenResponse)
def login(req: LoginRequest, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.email == req.email).first()
    if not user or not verify_password(req.password, user.password_hash):
        raise HTTPException(status_code=401, detail="Invalid email or password")

    token = create_access_token(user.id)
    return TokenResponse(
        access_token=token,
        user={"id": user.id, "email": user.email, "name": user.name},
    )


@router.get("/me", response_model=UserResponse)
def get_me(user: User = Depends(get_current_user)):
    return UserResponse(id=user.id, email=user.email, name=user.name)


class UpdateSettingsRequest(BaseModel):
    vix_offset: float | None = None
    total_capital_inr: float | None = None        # Margin tracker (Batch A.2)
    max_daily_loss_inr: float | None = None       # Daily loss-stop (Batch C.1)


@router.get("/settings")
def get_settings(user: User = Depends(get_current_user)):
    settings = user.settings or {}
    return {
        "vix_offset": settings.get("vix_offset", 50),
        # `None`-able fields — frontend treats null as "not set / disabled"
        "total_capital_inr":  settings.get("total_capital_inr"),
        "max_daily_loss_inr": settings.get("max_daily_loss_inr"),
    }


@router.put("/settings")
def update_settings(
    req: UpdateSettingsRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    settings = dict(user.settings or {})   # copy so SQLAlchemy detects change
    if req.vix_offset is not None:
        settings["vix_offset"] = req.vix_offset
    # Capital and daily-loss are nullable on purpose: passing 0 disables both.
    # An empty/missing key leaves the prior value untouched.
    if req.total_capital_inr is not None:
        settings["total_capital_inr"] = float(req.total_capital_inr) if req.total_capital_inr > 0 else None
    if req.max_daily_loss_inr is not None:
        settings["max_daily_loss_inr"] = float(req.max_daily_loss_inr) if req.max_daily_loss_inr > 0 else None
    user.settings = settings
    db.commit()
    return {"message": "Settings updated", "settings": settings}


class UpdateProfileRequest(BaseModel):
    name: str | None = None
    email: str | None = None
    current_password: str | None = None
    new_password: str | None = None


@router.put("/profile", response_model=UserResponse)
def update_profile(
    req: UpdateProfileRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Update profile credentials. Email/password changes require current_password."""
    sensitive = (req.email and req.email != user.email) or req.new_password
    if sensitive:
        if not req.current_password or not verify_password(req.current_password, user.password_hash):
            raise HTTPException(status_code=403, detail="Current password is incorrect")

    if req.name and req.name.strip():
        user.name = req.name.strip()

    if req.email and req.email.strip() and req.email != user.email:
        clash = db.query(User).filter(User.email == req.email, User.id != user.id).first()
        if clash:
            raise HTTPException(status_code=400, detail="Email already in use")
        user.email = req.email.strip()

    if req.new_password:
        if len(req.new_password) < 6:
            raise HTTPException(status_code=400, detail="New password must be at least 6 characters")
        user.password_hash = hash_password(req.new_password)

    db.commit()
    db.refresh(user)
    return UserResponse(id=user.id, email=user.email, name=user.name)
