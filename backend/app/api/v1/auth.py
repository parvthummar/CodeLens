from fastapi import APIRouter, status

from app.schemas.auth import SignupRequest, LoginRequest, TokenResponse, UserResponse
from app.services import auth_service

router = APIRouter()

@router.post("/signup", response_model=UserResponse, status_code=status.HTTP_201_CREATED)
async def signup(request: SignupRequest) -> UserResponse:
    user = await auth_service.signup(request)
    return UserResponse(
        id=str(user.id),
        email=user.email,
        full_name=user.full_name,
        is_active=user.is_active,
        created_at=user.created_at
    )

@router.post("/login", response_model=TokenResponse)
async def login(request: LoginRequest) -> TokenResponse:
    return await auth_service.login(request)
