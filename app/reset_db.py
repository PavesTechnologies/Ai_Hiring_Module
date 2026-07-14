from app.db.database import engine, Base

# Import model modules
import app.models.identity
import app.models.embeddings
import app.models.jd.job_descriptions
import app.models.campaigns
import app.models.compliance
import app.models.candidates
import app.models.pipeline
import app.models.async_tasks
import app.models.skills

print(Base.metadata.tables.keys())

Base.metadata.drop_all(bind=engine)
Base.metadata.create_all(bind=engine)