from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from backend.app.database import get_db
from backend.app.models import User,Tenant
from backend.app.schemas.users import UserCreate, UserResponse, UserLogin, TeamMemberCreate, RoleUpdate
from backend.app.utils.security import hash_password, verify_password
from backend.app.utils.jwt import create_access_token,get_current_user
from backend.app.utils.roles import require_roles

router = APIRouter(prefix="/users", tags=["Users"])


@router.post("/register", response_model=UserResponse)
def register_user(user: UserCreate, db: Session = Depends(get_db)):
    existing_user = db.query(User).filter(User.email == user.email).first()

    if existing_user:
        raise HTTPException(
            status_code=400,
            detail="Email already registered"
        )

    new_tenant = Tenant(
        name=user.company_name
    )

    db.add(new_tenant)
    db.flush()

    new_user = User(
        tenant_id=new_tenant.id,
        name=user.name,
        email=user.email,
        password_hash=hash_password(user.password),
        role="owner",
    )

    db.add(new_user)
    db.commit()
    db.refresh(new_user)

    return new_user


@router.post("/login")
def login_user(user: UserLogin, db: Session = Depends(get_db)):
    existing_user = db.query(User).filter(User.email == user.email).first()

    if not existing_user:
        raise HTTPException(status_code=401, detail="Invalid email or password")

    if not verify_password(user.password, existing_user.password_hash):
        raise HTTPException(status_code=401, detail="Invalid email or password")

    access_token = create_access_token(
        data={
            "sub": str(existing_user.id),
            "tenant_id": existing_user.tenant_id,
            "role": existing_user.role,
        }
    )

    return {"access_token": access_token, "token_type": "bearer"}


@router.get("/members", response_model=list[UserResponse])
def get_members(
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    members = (
        db.query(User)
        .filter(User.tenant_id == current_user["tenant_id"])
        .all()
    )

    return members


@router.post("/members", response_model=UserResponse)
def add_team_member(
    member: TeamMemberCreate,
    db: Session = Depends(get_db),
    current_user: dict = Depends(
        require_roles("owner", "admin")
    ),
):
    existing_user = (
        db.query(User)
        .filter(User.email == member.email)
        .first()
    )

    if existing_user:
        raise HTTPException(
            status_code=400,
            detail="Email already registered"
        )

    new_member = User(
        tenant_id=current_user["tenant_id"],
        name=member.name,
        email=member.email,
        password_hash=hash_password(member.password),
        role=member.role,
    )

    db.add(new_member)
    db.commit()
    db.refresh(new_member)

    return new_member


@router.patch("/members/{user_id}/role", response_model=UserResponse)
def update_member_role(
    user_id: int,
    role_data: RoleUpdate,
    db: Session = Depends(get_db),
    current_user: dict = Depends(
        require_roles("owner", "admin")
    ),
):
    member = (
        db.query(User)
        .filter(
            User.id == user_id,
            User.tenant_id == current_user["tenant_id"],
        )
        .first()
    )

    if not member:
        raise HTTPException(
            status_code=404,
            detail="User not found"
        )

    member.role = role_data.role

    db.commit()
    db.refresh(member)

    return member