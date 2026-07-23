from fastapi import FastAPI
from pydantic import BaseModel
from typing import List

app = FastAPI()

class Tea(BaseModel):
    id: int
    name: str
    origin: str

teas : List[Tea] = []

@app.get("/")
def read_root():
    return {"message":"welcome to Tea house!"}

@app.get("/teas")
def get_teas():
    return teas

@app.post("/teas")
def add_teas(tea: Tea):
    teas.append(tea)
    return tea

@app.put("/teas/{tea_id}")
def update_tea(tea_id: int, updated_tea: Tea):
    for idx, tea in enumerate(teas):
        if tea.id == tea_id:
            teas[idx] = updated_tea
            return updated_tea
    return {"error":f"tea with id: {tea_id} not found"}
    
