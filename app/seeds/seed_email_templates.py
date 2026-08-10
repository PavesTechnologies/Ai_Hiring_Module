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
    # M12 (Workflow & Interview Scheduling): placeholder copy only - subject
    # and name are [SEED]-tagged so these are easy to find and replace with
    # real copy once M12's actual notification content is finalized.
    # {candidate_name}/{job_title}/{interview_date}/{interview_time}/
    # {interview_mode}/{interviewer_name} placeholders only, matching this
    # file's existing no-internal-detail-leakage convention.
    #
    # NOTE: no interview_schedules table exists yet (M12 follow-up work).
    # These placeholder names are a reasonable guess at what that table will
    # expose, but whoever wires up the actual render/send call needs to pass
    # a dict with exactly these keys - expect to revisit/rename one or two
    # once that table and the send-email code path are actually built.
    {
        "trigger_event": EmailTriggerEvent.INTERVIEW_SCHEDULED,
        "name": "[SEED] Interview Scheduled",
        "subject": "[SEED] Your interview for {job_title} has been scheduled",
        "body_template": (
            "Dear {candidate_name},\n\n"
            "Your interview for the {job_title} position has been scheduled.\n\n"
            "Date: {interview_date}\n"
            "Time: {interview_time}\n"
            "Mode: {interview_mode}\n"
            "Interviewer: {interviewer_name}\n\n"
            "Please let us know as soon as possible if this time does not work for you.\n\n"
            "Best regards,\n"
            "The Hiring Team"
        ),
        "is_active": True,
    },
    {
        "trigger_event": EmailTriggerEvent.INTERVIEW_RESCHEDULED,
        "name": "[SEED] Interview Rescheduled",
        "subject": "[SEED] Your interview for {job_title} has been rescheduled",
        "body_template": (
            "Dear {candidate_name},\n\n"
            "Your interview for the {job_title} position has been rescheduled.\n\n"
            "New date: {interview_date}\n"
            "New time: {interview_time}\n"
            "Mode: {interview_mode}\n"
            "Interviewer: {interviewer_name}\n\n"
            "We apologize for any inconvenience and look forward to speaking with you.\n\n"
            "Best regards,\n"
            "The Hiring Team"
        ),
        "is_active": True,
    },
    {
        "trigger_event": EmailTriggerEvent.INTERVIEW_CANCELLED,
        "name": "[SEED] Interview Cancelled",
        "subject": "[SEED] Your interview for {job_title} has been cancelled",
        "body_template": (
            "Dear {candidate_name},\n\n"
            "Your interview for the {job_title} position, previously scheduled for "
            "{interview_date} at {interview_time}, has been cancelled.\n\n"
            "We will reach out if the interview needs to be rescheduled.\n\n"
            "Best regards,\n"
            "The Hiring Team"
        ),
        "is_active": True,
    },
    {
        "trigger_event": EmailTriggerEvent.CANDIDATE_SELECTED,
        "name": "[SEED] Candidate Selected",
        "subject": "[SEED] Congratulations - you have been selected for {job_title}",
        "body_template": (
            "Dear {candidate_name},\n\n"
            "Congratulations! We are pleased to inform you that you have been selected "
            "for the {job_title} position.\n\n"
            "A member of our team will be in touch shortly with next steps.\n\n"
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
