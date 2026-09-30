import uuid

from app.db.session import SessionLocal
from app.models.email import EmailTemplate, EmailTriggerEvent

db = SessionLocal()

# HTML template design fix: every EmailTemplate.body_template is now a full
# HTML document built from the same card shell (gradient bar, header,
# footer) - see app/tasks/email_tasks.py for the corresponding send-side
# change (SESEmailClient now sends these as an Html body, with every
# context value HTML-escaped and \n converted to <br> before formatting).
# {placeholder} tokens below are deliberately literal - they're filled in
# at send time by .format(**context), not by this seed script.
_CARD_SHELL = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>__HEADING__</title>
</head>
<body style="margin:0; padding:0; background:#f3f5f9; font-family:Arial, Helvetica, sans-serif;">
<table width="100%" cellpadding="0" cellspacing="0" style="padding:40px 0; background:#f3f5f9;">
<tr>
<td align="center">
<table width="640" cellpadding="0" cellspacing="0" style="background:#ffffff; border-radius:10px; border:1px solid #e0e4ec;">
<tr>
<td style="height:8px; padding:0; margin:0; line-height:8px;">
<!--[if gte mso 9]>
<v:rect xmlns:v="urn:schemas-microsoft-com:vml" fill="true" stroke="false" style="width:640px;height:8px;">
<v:fill type="gradient" angle="90" color="#0A1A44" color2="#1A4DFF" />
</v:rect>
<![endif]-->
<div style="background:linear-gradient(90deg, #0A1A44, #3B0E57, #1A4DFF); height:8px; width:100%;"></div>
</td>
</tr>
<tr>
<td style="padding:32px 40px 20px;">
<h2 style="margin:0; font-size:22px; color:#0A1A44; font-weight:700;">__HEADING__</h2>
<p style="margin:8px 0 0; font-size:14px; color:#666;">Notification from Paves AI Hiring Module</p>
</td>
</tr>
<tr>
<td style="padding:10px 40px 30px; font-size:15px; color:#444; line-height:1.7;">
__BODY__
</td>
</tr>
<tr>
<td style="background:#f6f7fb; text-align:center; padding:14px; font-size:12px; color:#888;">
© 2026 Paves Global Infotech Private Limited. All rights reserved.
</td>
</tr>
</table>
</td>
</tr>
</table>
</body>
</html>"""


def _card(heading: str, body_html: str) -> str:
    return _CARD_SHELL.replace("__HEADING__", heading).replace("__BODY__", body_html)


def _section_title(text: str) -> str:
    return (
        '<div style="margin:0 0 15px;">'
        '<div style="font-size:15px; font-weight:700; color:#0A1A44; '
        'border-left:4px solid #1A4DFF; padding-left:10px;">' + text + "</div></div>"
    )


def _details_box(rows) -> str:
    row_html = "".join(
        '<tr><td style="padding:8px 0; width:150px; font-weight:bold;">' + label + "</td>"
        '<td style="padding:8px 0;">' + value + "</td></tr>"
        for label, value in rows
    )
    return (
        '<table width="100%" cellpadding="0" cellspacing="0" '
        'style="background:#fafbff; border:1px solid #e2e6ef; border-radius:8px;">'
        '<tr><td style="padding:20px 25px;">'
        '<table width="100%" cellpadding="0" cellspacing="0" style="font-size:14px; color:#333;">'
        + row_html
        + "</table></td></tr></table>"
    )


def _cta_button(href: str, label: str) -> str:
    return (
        '<div style="text-align:center; margin:32px 0;">'
        '<a href="' + href + '" style="'
        "background:#0A1A44; padding:12px 32px; color:#ffffff !important; "
        "font-weight:600; font-size:15px; border-radius:6px; text-decoration:none; "
        "display:inline-block; border:1px solid #1A4DFF; font-family:Arial, Helvetica, sans-serif;"
        '">' + label + "</a></div>"
    )


# M07-E03 S02 T02: the standard candidate-rejection template - only
# {candidate_name}/{job_title} placeholders. Deliberately generic: never
# mentions missing skills, experience/education gaps, or any other
# internal rejection reason.
_TEMPLATES = [
    {
        "trigger_event": EmailTriggerEvent.CANDIDATE_REJECTED,
        "name": "Standard Candidate Rejection",
        "subject": "Update on your application for {job_title}",
        "body_template": _card(
            "Update on Your Application",
            '<p style="margin:0 0 18px;">Dear {candidate_name},</p>'
            '<p style="margin:0 0 18px;">Thank you for your interest in the {job_title} position '
            "and for taking the time to apply.</p>"
            '<p style="margin:0 0 18px;">After careful review, we have decided not to move forward '
            "with your application at this time.</p>"
            '<p style="margin:0;">We appreciate the effort you put into your application and '
            "encourage you to apply for future opportunities that match your skills and "
            "experience.</p>",
        ),
        "is_active": True,
    },
    # Epic 4 (M05-E04) Phase D0: sent to the uploader + all active HR_ADMIN
    # when a task reaches DEAD (retries exhausted) - D11 wires the actual
    # send. {filename}/{campaign_name}/{error_reason} placeholders only,
    # matching this file's existing no-internal-detail-leakage convention.
    # Ops-facing, not candidate-facing - no {candidate_name} greeting.
    {
        "trigger_event": EmailTriggerEvent.UPLOAD_PERMANENTLY_FAILED,
        "name": "Upload Permanently Failed",
        "subject": "Upload failed: {filename}",
        "body_template": _card(
            "Upload Failed - Action Needed",
            '<p style="margin:0 0 18px;">Hello,</p>'
            '<p style="margin:0 0 25px;">A resume upload could not be processed after multiple '
            "attempts and requires manual attention.</p>"
            + _section_title("Upload Details")
            + _details_box(
                [
                    ("File", "{filename}"),
                    ("Campaign", "{campaign_name}"),
                    ("Reason", "{error_reason}"),
                ],
            )
            + '<p style="margin:25px 0 0;">Please review this upload in the Failed Uploads '
            "section of the campaign.</p>",
        ),
        "is_active": True,
    },
    # M12 (Workflow & Interview Scheduling). {candidate_name}/{job_title}/
    # {interview_date}/{interview_time}/{interview_mode}/{interviewer_name}/
    # {meeting_info} placeholders, matching this file's existing
    # no-internal-detail-leakage convention. {meeting_info} is precomputed
    # by candidate_notification_emails.py's _meeting_info_line - a real
    # "Join here: {link}" line for TEAMS/MEET once the calendar API
    # actually returned one, a graceful fallback if it hasn't yet, the
    # location for ONSITE, or a phone-call notice for PHONE - str.format
    # has no conditional-block syntax, so this branch lives in the context
    # builder, not the template string itself.
    {
        "trigger_event": EmailTriggerEvent.INTERVIEW_SCHEDULED,
        "name": "Interview Scheduled",
        "subject": "Your interview for {job_title} has been scheduled",
        "body_template": _card(
            "Your Interview Has Been Scheduled",
            '<p style="margin:0 0 18px;">Dear {candidate_name},</p>'
            '<p style="margin:0 0 25px;">Your interview for the {job_title} position has been '
            "scheduled. Details are below.</p>"
            + _section_title("Interview Details")
            + _details_box(
                [
                    ("Date", "{interview_date}"),
                    ("Time", "{interview_time}"),
                    ("Mode", "{interview_mode}"),
                    ("Interviewer", "{interviewer_name}"),
                ],
            )
            + _section_title("Meeting Link")
            + '<table width="100%" cellpadding="0" cellspacing="0" '
            'style="background:#fafbff; border:1px solid #e2e6ef; border-radius:8px; margin-top:15px;">'
            '<tr><td style="padding:20px 25px; font-size:14px; color:#333;">{meeting_info}</td></tr>'
            "</table>",
        ),
        "is_active": True,
    },
    {
        "trigger_event": EmailTriggerEvent.INTERVIEW_RESCHEDULED,
        "name": "Interview Rescheduled",
        "subject": "Your interview for {job_title} has been rescheduled",
        "body_template": _card(
            "Your Interview Has Been Rescheduled",
            '<p style="margin:0 0 18px;">Dear {candidate_name},</p>'
            '<p style="margin:0 0 25px;">Your interview for the {job_title} position has been '
            "rescheduled. Updated details are below.</p>"
            + _section_title("Interview Details")
            + _details_box(
                [
                    ("New Date", "{interview_date}"),
                    ("New Time", "{interview_time}"),
                    ("Mode", "{interview_mode}"),
                    ("Interviewer", "{interviewer_name}"),
                ],
            )
            + _section_title("Meeting Link")
            + '<table width="100%" cellpadding="0" cellspacing="0" '
            'style="background:#fafbff; border:1px solid #e2e6ef; border-radius:8px; margin-top:15px;">'
            '<tr><td style="padding:20px 25px; font-size:14px; color:#333;">{meeting_info}</td></tr>'
            "</table>"
            '<p style="margin:25px 0 0;">We apologize for any inconvenience and look forward to '
            "speaking with you.</p>",
        ),
        "is_active": True,
    },
    {
        "trigger_event": EmailTriggerEvent.INTERVIEW_CANCELLED,
        "name": "Interview Cancelled",
        "subject": "Your interview for {job_title} has been cancelled",
        "body_template": _card(
            "Your Interview Has Been Cancelled",
            '<p style="margin:0 0 18px;">Dear {candidate_name},</p>'
            '<p style="margin:0 0 25px;">Your interview for the {job_title} position, previously '
            "scheduled as below, has been cancelled.</p>"
            + _section_title("Previously Scheduled")
            + _details_box([("Date", "{interview_date}"), ("Time", "{interview_time}")])
            + '<p style="margin:25px 0 0;">We will reach out if the interview needs to be '
            "rescheduled.</p>",
        ),
        "is_active": True,
    },
    {
        "trigger_event": EmailTriggerEvent.CANDIDATE_SELECTED,
        "name": "Candidate Selected",
        "subject": "Congratulations - you have been selected for {job_title}",
        "body_template": _card(
            "Congratulations!",
            '<p style="margin:0 0 18px;">Dear {candidate_name},</p>'
            '<p style="margin:0 0 18px;">Congratulations! We are pleased to inform you that you '
            "have been selected for the {job_title} position.</p>"
            '<p style="margin:0;">A member of our team will be in touch shortly with next steps.</p>',
        ),
        "is_active": True,
    },
    # Epic 5 Step 4: placeholder copy only, [SEED]-tagged like the M12
    # entries below. The ONLY non-candidate recipient template in this
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
    # actual feedback form page should confirm/correct this path. Kept as
    # a real CTA button (unlike INTERVIEW_SCHEDULED's, which was removed)
    # since giving feedback IS the entire point of this email - there's no
    # other action to take.
    {
        "trigger_event": EmailTriggerEvent.INTERVIEW_FEEDBACK_REQUESTED,
        "name": "[SEED] Interview Feedback Requested",
        "subject": "[SEED] Feedback requested: {candidate_name} - {job_title}",
        "body_template": _card(
            "Feedback Requested",
            '<p style="margin:0 0 18px;">Hello {recipient_name},</p>'
            '<p style="margin:0 0 18px;">Thank you for interviewing {candidate_name} for the '
            "{job_title} position.</p>"
            '<p style="margin:0 0 0;">Please share your feedback using the button below:</p>'
            + _cta_button("{feedback_link}", "Give Feedback")
            + '<p style="margin:0; font-size:13px; color:#888;">This link is unique to you and '
            "will expire in 14 days.</p>",
        ),
        "is_active": True,
    },
    # Interviewer lifecycle follow-up: [SEED]-tagged like the entries
    # above. {notes_block} is precomputed by interview_interviewer_
    # lifecycle_emails.py as either "" or "\n\nNotes: {the actual notes}" -
    # str.format has no conditional-block syntax, so "render if present,
    # omit cleanly if absent" is resolved at context-build time, not in
    # this template string itself. Placed inline (no wrapping element) so
    # an empty string leaves no visible gap when there are no notes.
    {
        "trigger_event": EmailTriggerEvent.INTERVIEW_INTERVIEWER_INVITATION,
        "name": "[SEED] Interview Interviewer Invitation",
        "subject": "[SEED] You're invited to interview {candidate_name} - {job_title}",
        "body_template": _card(
            "You've Been Added as an Interviewer",
            '<p style="margin:0 0 18px;">Hello {recipient_name},</p>'
            "<p style=\"margin:0 0 25px;\">You've been added as an interviewer for "
            "{candidate_name}'s interview for the {job_title} position.</p>"
            + _section_title("Interview Details")
            + _details_box(
                [("Date", "{interview_date}"), ("Time", "{interview_time}"), ("Mode", "{interview_mode}")],
            )
            + "{notes_block}",
        ),
        "is_active": True,
    },
    # Reschedule-notification gap fix: sent to an interviewer who was
    # already invited to this round and stays on it through a reschedule -
    # INVITATION won't fire again for them (deduped per (interview_
    # schedule_id, interviewer_id)), so without this they'd never learn
    # the time changed. {interview_date}/{interview_time}/{interview_mode}
    # only - no {notes_block}, since interview_interviewer_lifecycle_
    # emails.py's queue_interview_interviewer_rescheduled_email doesn't
    # pass one (see that function's own docstring).
    {
        "trigger_event": EmailTriggerEvent.INTERVIEW_INTERVIEWER_RESCHEDULED,
        "name": "[SEED] Interview Interviewer Rescheduled",
        "subject": "[SEED] Interview rescheduled: {candidate_name} - {job_title}",
        "body_template": _card(
            "Your Interview Assignment Has Been Rescheduled",
            '<p style="margin:0 0 18px;">Hello {recipient_name},</p>'
            '<p style="margin:0 0 25px;">The interview with {candidate_name} for the {job_title} '
            "position has been rescheduled. Updated details are below.</p>"
            + _section_title("Interview Details")
            + _details_box(
                [
                    ("New Date", "{interview_date}"),
                    ("New Time", "{interview_time}"),
                    ("Mode", "{interview_mode}"),
                ],
            ),
        ),
        "is_active": True,
    },
    {
        "trigger_event": EmailTriggerEvent.INTERVIEW_INTERVIEWER_REMOVED,
        "name": "[SEED] Interview Interviewer Removed",
        "subject": "[SEED] Update on {candidate_name}'s interview - {job_title}",
        "body_template": _card(
            "Update on Your Interview Assignment",
            '<p style="margin:0 0 18px;">Hello {recipient_name},</p>'
            "<p style=\"margin:0;\">You're no longer needed for {candidate_name}'s interview for "
            "the {job_title} position. Thank you for your time.</p>",
        ),
        "is_active": True,
    },
    # {reason_block} follows the same precomputed-context pattern as
    # {notes_block} above - "" or "\n\nReason: {the actual reason}".
    {
        "trigger_event": EmailTriggerEvent.INTERVIEW_INTERVIEWER_CANCELLED,
        "name": "[SEED] Interview Interviewer Cancelled",
        "subject": "[SEED] Interview cancelled: {candidate_name} - {job_title}",
        "body_template": _card(
            "Interview Cancelled",
            '<p style="margin:0 0 18px;">Hello {recipient_name},</p>'
            '<p style="margin:0 0 25px;">The interview with {candidate_name} for the {job_title} '
            "position, previously scheduled as below, has been cancelled.</p>"
            + _section_title("Previously Scheduled")
            + _details_box([("Date", "{interview_date}"), ("Time", "{interview_time}")])
            + "{reason_block}",
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
            if existing.subject != template["subject"] or existing.body_template != template["body_template"]:
                existing.subject = template["subject"]
                existing.body_template = template["body_template"]
                print(f"Updated active template for {template['trigger_event'].value}")
            else:
                print(f"Active template already up to date for {template['trigger_event'].value}")
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
