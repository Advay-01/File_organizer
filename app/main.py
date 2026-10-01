from fastapi import FastAPI

app = FastAPI(title="AI File Organizer")

@app.get("/health")
def health():
    return {"status": "ok"}