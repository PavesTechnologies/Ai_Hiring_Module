# Hard-negative test set — AIRS deterministic layer

Ten synthetic resumes designed to trip up specific rules. All people, companies and institutions are invented; contact details use `example.invalid`. "Present" means October 2026.

They're scored against the two calibrated Phase 3 JDs:
- **JD-D, ServiceNow ITSM Consultant** (Domain template): core ServiceNow ITSM, Incident Management, Change Management (ITSM), Service Catalog; 1 may be missing; minimum 4 years; education Preferred.
- **JD-F, Junior Python Developer** (Fresher template): core Python, SQL, REST APIs, Git; 1 may be missing; minimum 1 year, internships count 100%; education Mandatory (Computer Science family).

Unlike the Phase 3 set, several of these are **expected to fail today**. The point is to find where the rules are too generous, not to reach 10 of 10.

---

## 1. Instructions for Claude Code

- Create one .docx per resume in `tests/fixtures/hard_negatives/` (`H1.docx` … `H10.docx`), text exactly as written, simple formatting.
- Create `tests/fixtures/hard_negatives/expectations.json` from the table in section 4 (fields: id, jd, trap, expected_outcome, expected_core, reason, likely_system_result).
- Make two zips (git-ignored): `hn_domain_H1-H6.zip` (H1–H6) and `hn_fresher_H7-H10.zip` (H7–H10).
- Keep this file as `tests/fixtures/hard_negatives/README.md`. Commit on its own branch, merge into RMe.
- Don't upload anything. No email.

---

## 2. Resumes for JD-D (ServiceNow ITSM Consultant)

### H1 — Lavanya Krishnan

lavanya.krishnan@example.invalid · +91 00000 00101 · Bengaluru

**Summary**
ServiceNow developer with 5 years on HR Service Delivery.

**Education**
B.Tech, Information Technology — Silver Oak Institute of Engineering, 2021

**Experience**
*ServiceNow HRSD Developer* — Peoplefirst Solutions · Sep 2021 – present
- Configured HR case management and HR service lifecycle flows on ServiceNow HR Service Delivery
- Built HR catalog items in Employee Center for onboarding, leave and payroll queries
- Designed lifecycle events for joiners, movers and leavers
- Wrote Glide scripts and business rules for HR case assignment

**Certifications**
ServiceNow Certified Implementation Specialist – HR Service Delivery

**Skills**
ServiceNow HRSD, Employee Center, Flow Designer, JavaScript, HR case management

---

### H2 — Siddharth Bose

siddharth.bose@example.invalid · +91 00000 00102 · Pune

**Summary**
Salesforce Service Cloud consultant with 6 years of CRM implementation experience.

**Education**
B.E., Computer Engineering — Deccan Plateau College of Engineering, 2020

**Experience**
*Senior Salesforce Consultant* — Cloudharbor Partners · Jan 2023 – present
- Configured case management and incident escalation rules in Salesforce Service Cloud
- Deployed changes to production using change sets and Copado release pipelines
- Built a self-service request catalog in Experience Cloud for customer support

*Salesforce Developer* — Cloudharbor Partners · Jul 2020 – Dec 2022
- Wrote Apex triggers and Lightning components
- Set up entitlements and milestones for support SLAs

**Certifications**
Salesforce Certified Service Cloud Consultant; Salesforce Certified Administrator

**Skills**
Salesforce Service Cloud, Apex, Lightning Web Components, Copado, SLA management

---

### H3 — Gaurav Thakur

gaurav.thakur@example.invalid · +91 00000 00103 · Ahmedabad

**Summary**
Construction project manager with 9 years delivering commercial building projects.

**Education**
B.E., Civil Engineering — Sabarmati Institute of Technology, 2017

**Experience**
*Senior Project Manager* — Riverstone Builders · Jan 2021 – present
- Chaired the change control board for scope change requests and variation orders
- Investigated site safety incidents and filed incident reports with the safety officer
- Maintained the approved materials catalogue and vendor rate cards

*Project Engineer* — Riverstone Builders · Sep 2017 – Dec 2020
- Planned site schedules and coordinated subcontractors

**Certifications**
PMP — Project Management Professional

**Skills**
Project Management, Contract Management, Primavera P6, AutoCAD, Site Safety

---

### H4 — Pooja Venkatesh

pooja.venkatesh@example.invalid · +91 00000 00104 · Chennai

**Summary**
Business analyst with 6 years in retail banking technology projects.

**Education**
MBA, Finance — Coromandel School of Management, 2020

**Experience**
*Senior Business Analyst* — Bayline Bank · Jul 2020 – present
- Raised service requests and access requests through the ServiceNow portal for the project team
- Attended the weekly CAB as the business approver for release changes affecting the loans platform
- Wrote user stories and acceptance criteria for the mobile banking app
- Ran user acceptance testing with branch staff

**Skills**
Requirements Analysis, User Stories, UAT, JIRA, SQL, Stakeholder Management

---

### H5 — Mohammed Rizwan

mohammed.rizwan@example.invalid · +91 00000 00105 · Hyderabad

**Summary**
Network engineer with 7 years supporting enterprise networks.

**Education**
B.Tech, Electronics and Communication Engineering — Golconda College of Engineering, 2019

**Experience**
*Senior Network Engineer* — Skybridge Networks · Jun 2022 – present
- Resolved P1 and P2 network incidents logged in ServiceNow
- Raised change requests for firewall and routing changes and presented them at CAB
- Configured Cisco and Palo Alto devices

*Network Engineer* — Skybridge Networks · Jul 2019 – May 2022
- Monitored WAN links and handled planned maintenance

**Certifications**
ITIL 4 Foundation; CCNP Enterprise; ServiceNow Certified System Administrator (appearing, Dec 2026)

**Skills**
Cisco IOS, Palo Alto, BGP, OSPF, Network Monitoring

---

### H6 — Tarun Mehta

tarun.mehta@example.invalid · +91 00000 00106 · Mumbai

**Summary**
Inside sales professional with 6 years selling IT services.

**Education**
B.Com — Harbour City College of Commerce, 2020

**Experience**
*Inside Sales Executive* — Nimbus IT Services · Jul 2020 – present
- Sold ServiceNow ITSM implementation services to mid-market clients
- Ran product demos of incident and change modules with the presales team
- Managed a pipeline of 60+ accounts in Salesforce CRM

**Skills**
ServiceNow ITSM, Incident Management, Change Management, Service Catalog, CMDB, ITIL, Salesforce CRM, Negotiation

---

## 3. Resumes for JD-F (Junior Python Developer)

### H7 — Nikhil Patil

nikhil.patil@example.invalid · +91 00000 00107 · Nagpur

**Summary**
Computer Science graduate with a year of software testing internships.

**Education**
B.Tech, Computer Science — GIT (Greenfield Institute of Technology), 2026 (CGPA 7.8)

**Experience**
*QA Intern* — Testwell Labs · Jul 2025 – Jun 2026
- Tested REST endpoints manually using Postman
- Logged defects in Jira and retested fixes
- Wrote test cases and test reports in Excel

**Skills**
Manual Testing, Postman, Jira, Excel, Test Case Design

---

### H8 — Shreya Banerjee

shreya.banerjee@example.invalid · +91 00000 00108 · Kolkata

**Summary**
Computer Science graduate with a year of Java backend internships.

**Education**
B.Tech, Computer Science and Engineering — Hooghly Institute of Technology, 2026 (CGPA 8.7)

**Experience**
*Java Developer Intern* — Corelink Systems · Jul 2025 – Jun 2026
- Built REST APIs in Java with Spring Boot
- Wrote SQL queries and stored procedures on MySQL
- Used Git with pull requests and code reviews
- Wrote JUnit tests for service classes

**Skills**
Java, Spring Boot, REST APIs, SQL, MySQL, Git, JUnit

---

### H9 — Karthika Suresh

karthika.suresh@example.invalid · +91 00000 00109 · Trivandrum

**Summary**
Commerce graduate who moved into software through a Python bootcamp.

**Education**
B.Com — Backwaters College of Arts and Commerce, 2025
Full-Stack Python Bootcamp (6 months) — CodeCraft Academy, Jan 2026 – Jun 2026

**Experience**
*Python Developer Intern* — Lagoon Apps · Jul 2026 – present
- Built REST APIs in Flask with PostgreSQL
- Wrote SQL queries for reporting
- Used Git and GitHub pull requests

**Projects**
- *Invoice Tracker*: Python, Flask, PostgreSQL, deployed with Docker

**Skills**
Python, SQL, REST APIs, Git, Flask, PostgreSQL, Docker

---

### H10 — Varun Reddy

varun.reddy@example.invalid · +91 00000 00110 · Hyderabad

**Summary**
Computer Science graduate with a year of technical support experience.

**Education**
B.Tech, Computer Science — Charminar Institute of Technology, 2026 (CGPA 7.4)

**Experience**
*Technical Support Intern* — Helpdesk Pro Services · Jul 2025 – Jun 2026
- Resolved customer tickets for a SaaS billing product
- Reset passwords and escalated bugs to the engineering team

**Skills**
Proficient: MS Office, customer communication, ticketing tools
Currently learning: Python, SQL, Git, REST APIs

---

## 4. Expected outcomes

"Expected" is what a recruiter would want. "Likely system result" is my guess at what today's rules do, so the comparison shows which traps actually work.

| ID | JD | Trap | Expected | Expected core | Reason | Likely system result |
|---|---|---|---|---|---|---|
| H1 | D | ServiceNow, but HR Service Delivery, not ITSM | REJECT | 1/4 (Service Catalog) | Real ServiceNow catalog work, but no ITSM, incident or change | Lines may be proposed for ServiceNow ITSM; the description should make the vote say no. Risk: HR cases read as incidents → 2/4 → still REJECT |
| H2 | D | Salesforce equivalents: "incident escalation", "change sets", "request catalog" | REJECT | 0/4 | Different platform; "change sets" is a Salesforce deployment term, not change management | **High risk**: Incident, Change and Catalog confirmed by paraphrase → 3/4 → PASS |
| H3 | D | Construction: "change control board", "safety incidents", "materials catalogue" | REJECT | 0/4 | Same words, unrelated meaning | Risk: CAB-like cue words resolve to ITSM; votes should say no |
| H4 | D | ServiceNow requester and CAB attendee, not implementer | REJECT | 0/4 | Uses the processes as a business user | Risk: Change (CAB cue) and ServiceNow ITSM (portal use) confirmed → 2/4 → still REJECT |
| H5 | D | Process participant with ITIL 4 and an *appearing* CSA | REJECT | 2/4 (Incident, Change as process experience) | ServiceNow ITSM and Catalog missing; the CSA isn't earned | Should be REJECT; checks the "appearing" rule blocks the CSA mapping |
| H6 | D | Salesperson with a keyword-stuffed skills list | REJECT | 0/4 | Sells ServiceNow, never configured it | **High risk**: exact matches in the Skills section auto-confirm → 4/4 → PASS |
| H7 | F | "GIT" is the college's acronym; REST only tested manually | REJECT | 0/4 (REST at most) | No Python, no SQL, no version control | Risk: GIT matched as Git, REST from "Tested REST endpoints" → 2/4 → HOLD |
| H8 | F | Strong Java developer, no Python at all | HOLD | 3/4 | A recruiter might consider her, but Python is the role's language | Likely PASS: one missing core skill is allowed. **Design question**: should some core skills be "never missing"? |
| H9 | F | Commerce degree plus bootcamp; 4 months of internship | REJECT | 4/4 | Education is mandatory (Computer Science family); B.Com isn't related | Should be REJECT on education; checks field families don't over-accept |
| H10 | F | "Currently learning" list; support internship | REJECT | 0/4 | Skills listed as being learned, never used | **High risk**: Skills-section matches auto-confirm → 4/4, internship counts → PASS |

### What the high-risk cases would mean

- **H2 (other platforms):** paraphrase detection can't tell ServiceNow work from equivalent work on another tool unless the vote checks the platform.
- **H6 and H10 (skills-section auto-confirm):** an exact skill name in a Skills section is auto-confirmed without a vote. That's what made Phase 1 stable, but it means a stuffed list or a "currently learning" list counts as proof.
- **H8 (critical core skills):** the "1 of 4 may be missing" allowance treats every core skill as equally optional, including the role's main language.
