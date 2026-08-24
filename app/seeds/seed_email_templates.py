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
    # Epic 5 Step 4: placeholder copy only, [SEED]-tagged like the M12
    # entries above. The ONLY non-candidate recipient template in this
    # file - {candidate_name}/{job_title} still refer to the candidate
    # being interviewed, not the recipient (see send_candidate_email_
    # task's EXTERNAL_INTERVIEWER branch). {recipient_name} is the
    # interviewer's own name - deliberately NOT reusing {interviewer_name}
    # (the INTERVIEW_SCHEDULED/RESCHEDULED templates' own placeholder,
    # which means "every interviewer on the round, joined" - a different
    # scope than "the one person this specific email is addressed to").
    # {feedback_link} is the signed, expiring token URL from
    # app.core.feedback_token, built by
    # interview_feedback_request_emails.py as
    # f"{FRONTEND_BASE_URL}/interview-feedback/{token}" - that frontend
    # route path is an assumption, not confirmed against a real frontend
    # page (none exists yet as of this session); whoever builds the
    # actual feedback form page should confirm/correct this path.
    {
        "trigger_event": EmailTriggerEvent.INTERVIEW_FEEDBACK_REQUESTED,
        "name": "[SEED] Interview Feedback Requested",
        "subject": "[SEED] Feedback requested: {candidate_name} - {job_title}",
        "body_template": (
            "Hello {recipient_name},\n\n"
            "Thank you for interviewing {candidate_name} for the {job_title} position.\n\n"
            "Please share your feedback using the link below:\n"
            "{feedback_link}\n\n"
            "This link is unique to you and will expire in 14 days.\n\n"
            "Best regards,\n"
            "The Hiring Team"
        ),
        "is_active": True,
    },
    # Interviewer lifecycle follow-up: [SEED]-tagged like the entries
    # above. {notes_block} is precomputed by interview_interviewer_
    # lifecycle_emails.py as either "" or "\n\nNotes: {the actual notes}" -
    # str.format has no conditional-block syntax, so "render if present,
    # omit cleanly if absent" is resolved at context-build time, not in
    # this template string itself.
    {
        "trigger_event": EmailTriggerEvent.INTERVIEW_INTERVIEWER_INVITATION,
        "name": "[SEED] Interview Interviewer Invitation",
        "subject": "[SEED] You're invited to interview {candidate_name} - {job_title}",
        "body_template": (
            "Hello {recipient_name},\n\n"
            "You've been added as an interviewer for {candidate_name}'s interview for the "
            "{job_title} position.\n\n"
            "Date: {interview_date}\n"
            "Time: {interview_time}\n"
            "Mode: {interview_mode}"
            "{notes_block}\n\n"
            "Best regards,\n"
            "The Hiring Team"
        ),
        "is_active": True,
    },
    {
        "trigger_event": EmailTriggerEvent.INTERVIEW_INTERVIEWER_REMOVED,
        "name": "[SEED] Interview Interviewer Removed",
        "subject": "[SEED] Update on {candidate_name}'s interview - {job_title}",
        "body_template": (
            "Hello {recipient_name},\n\n"
            "You're no longer needed for {candidate_name}'s interview for the {job_title} "
            "position. Thank you for your time.\n\n"
            "Best regards,\n"
            "The Hiring Team"
        ),
        "is_active": True,
    },
    # {reason_block} follows the same precomputed-context pattern as
    # {notes_block} above - "" or "\n\nReason: {the actual reason}".
    {
        "trigger_event": EmailTriggerEvent.INTERVIEW_INTERVIEWER_CANCELLED,
        "name": "[SEED] Interview Interviewer Cancelled",
        "subject": "[SEED] Interview cancelled: {candidate_name} - {job_title}",
        "body_template": (
            "Hello {recipient_name},\n\n"
            "The interview with {candidate_name} for the {job_title} position, previously "
            "scheduled for {interview_date} at {interview_time}, has been cancelled."
            "{reason_block}\n\n"
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
