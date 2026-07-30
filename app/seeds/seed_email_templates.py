import uuid

from app.db.session import SessionLocal
from app.models.email import EmailTemplate, EmailTriggerEvent

db = SessionLocal()

# M07-E03 S02 T02: the standard candidate-rejection template - only
# {candidate_name}/{job_title} placeholders. Deliberately generic: never
# mentions missing skills, experience/education gaps, or any other
# internal rejection reason.
_TEMPLATES = [
    {
        "trigger_event": EmailTriggerEvent.CANDIDATE_REJECTED,
        "name": "Standard Candidate Rejection",
        "subject": "Update on your application for {job_title}",
        "body_template": (
            "Dear {candidate_name},\n\n"
            "Thank you for your interest in the {job_title} position and for taking the "
            "time to apply.\n\n"
            "After careful review, we have decided not to move forward with your "
            "application at this time.\n\n"
            "We appreciate the effort you put into your application and encourage you to "
            "apply for future opportunities that match your skills and experience.\n\n"
            "Best regards,\n"
            "The Hiring Team"
        ),
        "is_active": True,
    },
    # Epic 4 (M05-E04) Phase D0: sent to the uploader + all active HR_ADMIN
    # when a task reaches DEAD (retries exhausted) - D11 wires the actual
    # send. {filename}/{campaign_name}/{error_reason} placeholders only,
    # matching this file's existing no-internal-detail-leakage convention.
    {
        "trigger_event": EmailTriggerEvent.UPLOAD_PERMANENTLY_FAILED,
        "name": "Upload Permanently Failed",
        "subject": "Upload failed: {filename}",
        "body_template": (
            "Hello,\n\n"
            "A resume upload could not be processed after multiple attempts and requires manual attention.\n\n"
            "File: {filename}\n"
            "Campaign: {campaign_name}\n"
            "Reason: {error_reason}\n\n"
            "Please review this upload in the Failed Uploads section of the campaign.\n\n"
            "Best regards,\n"
            "The Hiring Team"
        ),
        "is_active": True,
    },
]

try:
    for template in _TEMPLATES:
        existing = (
            db.query(EmailTemplate)
            .filter(
                EmailTemplate.trigger_event == template["trigger_event"],
                EmailTemplate.is_active.is_(True),
            )
            .first()
        )
        if existing:
            print(f"Active template already exists for {template['trigger_event'].value}")
            continue

        db.add(EmailTemplate(id=uuid.uuid4(), **template))
        print(f"Added active template for {template['trigger_event'].value}")

    db.commit()
    print("\nEmail templates seeded successfully")

except Exception as e:
    db.rollback()
    print(f"Error seeding email templates: {e}")
    raise

finally:
    db.close()
