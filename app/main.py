from fastapi import FastAPI

from Blueprint_db import Base, engine

Base.metadata.create_all(bind=engine)

app = FastAPI()


@app.get("/")
def root() -> dict[str, str]:
    return {"message": "Hello, World!"}

