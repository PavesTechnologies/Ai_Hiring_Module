from app.schemas.jd.DuplicateJDInfo import DuplicateJDInfo


class DuplicateJDException(Exception):


    def __init__(self, existing_jd: DuplicateJDInfo):
        self.existing_jd = existing_jd
        super().__init__("Duplicate job description found.")
        
        
    
    