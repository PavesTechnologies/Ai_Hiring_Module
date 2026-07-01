from app.models.jd.job_descriptions import JobDescription


class DuplicateJDException(Exception):
    
    
    def __init__(self, existing_jd: JobDescription):
        self.existing_jd = existing_jd
        super().__init__("Duplicate job description found.")
        
        
    
    