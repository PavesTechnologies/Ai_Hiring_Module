# How Resume Uploads Work (and How We Catch Duplicates)

A explanation of what happens when someone uploads a resume — one at a time, or in a bulk ZIP batch — and how the system decides whether something is a duplicate and what to do about it.

---

## 1. Two Ways to Upload a Resume

**Single upload:** the user fills in a form (name, email, campaign) and attaches one file. Because the name and email are typed in upfront, the system already knows *who* this is supposed to be before it even looks at the file.

**Bulk upload (ZIP of many resumes):** the user just picks a campaign and uploads one ZIP file containing many resumes. Nobody tells the system who each resume belongs to — it has to open each file, read it, and figure out the candidate's name and email itself before it can do anything else.

That one difference — "do we already know who this is?" — is the reason bulk uploads take an extra step that single uploads don't need.

---

## 2. What Happens, Step by Step

### Single upload

1. Check the campaign is actually open (not paused, not closed, not full).
2. Check this candidate isn't already signed up for this same campaign. If they are, stop right here and tell the user — no file gets touched.
3. Check whether this exact file has been uploaded before anywhere in the system (more on this below).
4. Save the file, create (or reuse) the candidate's record, save the resume.
5. Link the candidate to the campaign.
6. Send the resume off in the background to be read and understood by AI — extracting skills, experience, etc.
7. Once that's done, the resume also gets scored against the job for that campaign.

Steps 1–5 happen instantly while the user waits; step 6 onward happens quietly in the background, and the user can check back later for the result.

### Bulk upload (ZIP)

1. The ZIP itself gets unpacked and each file inside it is saved individually.
2. For each file, the system reads it first — extracting text, then asking AI to pull out the candidate's name and contact details — because at this point it genuinely doesn't know who the resume belongs to.
3. Once it knows who the candidate is, it runs the exact same checks a single upload would: is this candidate already in this campaign? Is this exact file a duplicate?
4. If everything's fine, the candidate and resume get created and linked to the campaign, and the rest of the AI processing continues.
5. Every file in the batch is handled independently — one bad or duplicate file doesn't stop the others.

---

## 3. Three Different Kinds of "Duplicate"

This is the part that trips people up, because there isn't just one duplicate check — there are three, and they answer three different questions.

### A) "Have we seen this person before?" — checked across the whole system

Every candidate is identified by their email address. If someone uploads with an email that already exists anywhere in the system — in any campaign, from any past upload — we don't create a second copy of that person. We just reuse their existing record.

This is a **global** check. It has nothing to do with which campaign they're applying to.

### B) "Have we seen this exact file before?" — checked across the whole system

Separately from who the person is, the system also checks the file itself — literally, is this the exact same document, byte for byte, as one already sitting in our records? This has nothing to do with the name or email typed into the form. It's purely "is this file identical to one we already have."

This is also a **global** check, and it's the one with the most interesting behavior — see section 4.

### C) "Is this person already in this specific campaign?" — checked one campaign at a time

Once we know who the candidate is, we ask a narrower question: have they already applied to *this* job posting? A person can be in five different campaigns at once — that's fine and expected. What's not allowed is applying to the *same* campaign twice through a fresh upload. If they're already in it, the upload is rejected and the user is told so directly.

This is the only one of the three checks that's scoped to a single campaign — the other two look across everything.

---

## 4. The Interesting Case: What If the Exact Same File Shows Up Again?

Here's where it gets nuanced, because "the same file" can happen for two very different reasons, and the system treats them differently.

### Same person, uploading their own resume to a new campaign

This is completely normal — someone applies to Campaign A, then later applies to Campaign B with the same resume. Since check (C) above only blocks re-applying to the *same* campaign, this goes through fine. The system then asks (or, for a single upload, may ask the user to confirm): should we just reuse the same processed resume for the new campaign, or treat this as a fresh copy?

- **Reuse it:** nothing new is saved — the new campaign simply points at the exact same resume record that already exists, already read and scored. No extra work, no waiting.
- **Treat it as fresh:** a brand-new copy of the resume is saved under that person, and it goes through the full AI reading/scoring process all over again — even though the content is identical. This only happens if explicitly requested.

Either way, the original campaign's link to the original resume is left completely untouched. Nothing that already happened gets changed or undone.

### Same file, but claiming to be a different person

This is the important safeguard. Suppose someone uploads a file that is byte-for-byte identical to a resume we already have on file for "Person A" — but the form says the candidate's name is "Person B."

The system does **not** trust the name/email typed into the form in this case. It already knows, from the file itself, exactly whose resume this is — so it quietly ignores the claimed "Person B" identity and treats the upload as if Person A submitted it.

**Why?** Without this rule, anyone could take someone else's exact resume file, relabel it with a different name, and trick the system into creating a fake second identity around a document that's already known to belong to a real person. Blocking that is exactly the point of this check — it's a deliberate anti-impersonation safeguard, not a bug or an oversight.

The practical effect: you cannot create a genuinely new, separate candidate record by uploading a file that's identical to someone else's existing resume. If two people are supposed to be different, at least one of them needs to actually submit their own document — not a copy of someone else's.

---

## 5. Quick Summary Table

| Situation | What happens |
|---|---|
| New person, new file | Everything gets created fresh — new candidate, new resume, added to the campaign, processed normally. |
| Known person, new campaign, brand-new file | New resume version created for that person, added to the new campaign, processed normally. |
| Known person, same campaign, any file | **Blocked** — "you're already in this campaign." Nothing gets saved. |
| Known person, different campaign, identical file | Either reuse the existing resume (no reprocessing) or create a fresh copy that gets reprocessed — user's choice for single uploads; always reused automatically for bulk uploads. |
| Different claimed name, but identical file to someone else's | Treated as if the *original* person uploaded it, not the name typed in the form. If that original person is already in the target campaign, it's blocked the same way as any other re-application. |

---

## 6. One Thing Still Slightly Inconsistent

For single uploads, when someone is blocked because they're "already in this campaign," the message they get back includes helpful details — their existing application's ID, their candidate ID, which resume they already submitted, and whether they're allowed to update it instead.

For bulk uploads, the same block happens, but those extra details aren't included yet — the user just gets the plain message without the extra context. This is a known gap, not something dangerous, and can be fixed later if it turns out to matter for the bulk workflow too.
