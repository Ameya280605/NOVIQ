from fastapi import FastAPI,Depends
from sqlalchemy.orm import Session
from app.database import get_db

app = FastAPI()


@app.get("/")
def root(db: Session = Depends(get_db)):
    return {"message": "Welcome to Noviq"}