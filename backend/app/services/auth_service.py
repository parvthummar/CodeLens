from fastapi import HTTPException, status

from app.models.user import User
from app.schemas.auth import SignupRequest, LoginRequest, TokenResponse
from app.core.security import hash_password, verify_password, create_access_token

async def signup(data: SignupRequest) -> User:
    existing_user = await User.find_one(User.email == data.email)
    if existing_user:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Email already registered"
        )
    
    hashed_password = hash_password(data.password)
    user = User(
        email=data.email,
        hashed_password=hashed_password,
        full_name=data.full_name,
        is_active=True
    )
    await user.insert()
    return user

async def login(data: LoginRequest) -> TokenResponse:
    user = await User.find_one(User.email == data.email)
    if not user or not verify_password(data.password, user.hashed_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect email or password",
            headers={"WWW-Authenticate": "Bearer"},
        )
    
    access_token = create_access_token(data={"sub": user.email})
    return TokenResponse(access_token=access_token)
