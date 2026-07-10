from app.services.ai.preprocessing_service import PreprocessingService

service = PreprocessingService()

sample = """
Senior Python Backend Developer

Responsibilities
- Develop scalable REST APIs using FastAPI
- Design PostgreSQL database schemas
- Optimize Redis caching
- Build event-driven services using Kafka
- Collaborate with DevOps teams

Required Skills
- Python
- FastAPI
- PostgreSQL
- Redis
- Docker
- Kubernetes

Preferred Skills
- AWS
- Terraform

Experience
Minimum 5 years of backend development experience.

Education
Bachelor's degree in Computer Science or Information Technology.
"""

print(service.normalize(sample))