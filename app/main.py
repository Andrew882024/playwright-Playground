from fastapi import FastAPI

from Blueprint_db import Base, engine

from app.routes.fomo_sync import router as fomo_sync_router

Base.metadata.create_all(bind=engine)

app = FastAPI()
app.include_router(fomo_sync_router, prefix="/sync", tags=["fomo"])


@app.get("/")
def root() -> dict[str, str]:
    return {"message": "Hello, World!"}



