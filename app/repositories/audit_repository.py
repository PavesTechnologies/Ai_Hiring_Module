from sqlalchemy.orm import Session
from app.models.compliance import AuditLog


class AuditRepository:
    def __init__(self, db: Session):
        self.db= db
    
    
        
    
    def create(self, audit_log: AuditLog)-> AuditLog:
        self.db.add(audit_log)
        self.db.flush()
        self.db.refresh(audit_log)
        return audit_log
        
    
    def save(self):
        self.session.commit()