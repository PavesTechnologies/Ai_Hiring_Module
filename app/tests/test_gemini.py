from app.services.extractions.gemini_extraction_service import (
    GeminiExtractionService,
)

service = GeminiExtractionService()

jd = """
Senior Python Backend Developer

Must Have

Python
FastAPI
Redis
Docker

Experience : 5+ years

Bachelor Degree

Responsibilities

Develop APIs
"""

result = service.extract(jd)

print(result.model_dump_json(indent=4))