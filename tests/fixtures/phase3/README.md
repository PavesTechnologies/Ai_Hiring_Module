# Phase 3 test data — AIRS deterministic layer

All people, companies and institutions below are invented. Contact details use `example.invalid`.
Each resume is built to test one or more Phase 3 rules. The expected outcomes are in section 5.
Experience is computed from the role dates; "present" counts up to the day of the run. D3 and D5 are dated so that, as of 2026-10-14, they have exactly 4.5 and 3.5 years (the years their summaries state); later runs see more.

Contents:
1. Instructions for Claude Code
2. JD-F: Junior Python Developer (fresher)
3. JD-D: ServiceNow ITSM Consultant (domain)
4. Resumes F1–F8 (fresher) and D1–D8 (domain)
5. Calibration settings and expected outcomes

---

## 1. Instructions for Claude Code

- Create one .docx per JD and per resume from the sections below, in `tests/fixtures/phase3/` (`jd_fresher.docx`, `jd_domain.docx`, `F1.docx` … `F8.docx`, `D1.docx` … `D8.docx`). Keep the text exactly as written; simple, clean resume formatting (headings, bullets).
- Create `tests/fixtures/phase3/expectations.json` from the table in section 5 (fields: id, jd, tests, expected_outcome, expected_core, reason, current_system_risk).
- Keep this file as `tests/fixtures/phase3/README.md`.
- Everything is synthetic, so it can be committed. Don't upload anything or change the live database. No email.

---

## 2. JD-F — Junior Python Developer (fresher)

**Junior Python Developer**
Northwind Digital Labs · Bengaluru · Full-time

**About the role**
We are hiring junior Python developers to join our platform team. You will build and maintain backend services and internal APIs, write tests, and work with senior engineers in code reviews.

**Experience**
Minimum 1 year of hands-on development experience. Internships count towards this.

**Education**
B.E. / B.Tech in Computer Science.

**Must-have skills**
- Python
- SQL
- REST APIs
- Git

**Good to have**
- Django or FastAPI
- Unit testing with pytest
- Linux command line
- Docker

**Nice to have**
- AWS
- React

**Responsibilities**
- Build and maintain REST APIs in Python
- Write SQL queries and simple database migrations
- Write unit tests and fix bugs found in review
- Use Git for version control and pull requests

---

## 3. JD-D — ServiceNow ITSM Consultant (domain)

**ServiceNow ITSM Consultant**
Brightpath IT Services · Hyderabad · Full-time

**About the role**
We are looking for a ServiceNow consultant to implement and improve IT Service Management processes for our enterprise clients.

**Experience**
Minimum 4 years of experience, with at least 3 years on the ServiceNow platform.

**Education**
Bachelor's degree in any discipline.

**Must-have skills**
- ServiceNow ITSM
- Incident Management
- Change Management (IT change control, CAB)
- Service Catalog

**Good to have**
- Problem Management
- CMDB
- Flow Designer
- JavaScript (Glide scripting)
- SLA management

**Nice to have**
- ITIL 4 Foundation certification
- ServiceNow Certified System Administrator (CSA)
- ServiceNow Certified Implementation Specialist – ITSM (CIS-ITSM)

**Domain capabilities**
- Configure incident, problem and change workflows to match client processes
- Design service catalogue items and request fulfilment flows
- Integrate ServiceNow with monitoring tools through REST
- Support Change Advisory Board (CAB) processes and approvals

---

## 4. Resumes

### F1 — Aarav Menon

aarav.menon@example.invalid · +91 00000 00001 · Bengaluru

**Summary**
Computer Science graduate with a year of internship experience building Python backend services.

**Education**
B.Tech, Computer Science and Engineering — Lakeside Institute of Technology, 2026 (CGPA 8.4)

**Experience**
*Software Engineering Intern* — Cobalt Apps Pvt Ltd · Jan 2026 – Jun 2026
- Built REST APIs in FastAPI for an internal ticketing tool
- Wrote SQL queries and Alembic migrations on PostgreSQL
- Added pytest unit tests, raising coverage from 40% to 72%

*Backend Developer Intern* — Greenleaf Software · Jul 2025 – Dec 2025
- Maintained Django REST endpoints and fixed bugs from code review
- Used Git with feature branches and pull requests
- Containerised a service with Docker for local development

**Projects**
- *Library API*: Python, FastAPI, PostgreSQL, deployed on AWS EC2

**Skills**
Python, SQL, REST APIs, Git, FastAPI, Django, pytest, Docker, Linux, AWS

---

### F2 — Diya Raman

diya.raman@example.invalid · +91 00000 00002 · Chennai

**Summary**
Final-year Computer Science graduate who has built several backend projects.

**Education**
B.E., Computer Science — Riverside College of Engineering, 2026 (CGPA 8.1)

**Projects**
- *Campus Events API*: Designed and built a REST API in Python with Flask; stored events in PostgreSQL with hand-written SQL queries; version-controlled on GitHub with pull requests from two teammates.
- *Expense Splitter*: Python command-line tool reading and writing data with SQLite; unit tests with pytest.
- *Weather Dashboard*: Consumed public REST APIs and cached results; ran on a Linux VM.

**Achievements**
- Second place, college hackathon 2025

---

### F3 — Kabir Shetty

kabir.shetty@example.invalid · +91 00000 00003 · Pune

**Summary**
Information Technology graduate with a year of backend internships.

**Education**
B.Tech, Information Technology — Hillview Institute of Engineering, 2026 (CGPA 8.6)

**Experience**
*Python Developer Intern* — Quantra Systems · Jul 2025 – Jun 2026
- Developed REST APIs in Django REST Framework
- Wrote and optimised SQL queries on MySQL
- Wrote pytest unit tests for new endpoints
- Followed a Git pull-request workflow with code reviews

**Skills**
Python, SQL, REST APIs, Git, Django, pytest, Linux, Docker

---

### F4 — Meera Iyer

meera.iyer@example.invalid · +91 00000 00004 · Coimbatore

**Summary**
Electronics and Communication graduate who moved into software through internships and projects.

**Education**
B.E., Electronics and Communication Engineering — Southern Valley Engineering College, 2026 (CGPA 8.0)

**Experience**
*Software Intern* — Pixelforge Technologies · Jan 2026 – Jun 2026
- Built REST APIs in Python (FastAPI) for an IoT device dashboard
- Wrote SQL queries for device telemetry reports
- Used Git for version control

**Projects**
- *Sensor Data Logger*: Python service storing sensor readings in PostgreSQL, with pytest tests

**Skills**
Python, SQL, REST APIs, Git, FastAPI, pytest, Embedded C

---

### F5 — Rohan Das

rohan.das@example.invalid · +91 00000 00005 · Kolkata

**Summary**
Computer Science graduate interested in backend engineering and API design.

**Education**
B.Tech, Computer Science — Eastern Institute of Technology, 2026 (CGPA 7.9)

**Experience**
*Programme Intern, Platform Engineering* — Harbourline Tech · Jul 2025 – Jun 2026
- Developed RESTful API endpoints for the organisation's customer portal
- Wrote database queries and optimised slow reports
- Worked in a version-controlled codebase with code reviews

**Key Skills**
- Languages: Python & SQL
- RESTful API Design & Development
- Version Control (Git) & Code Review
- Unit Testing (pytest) & Debugging
- Containerisation (Docker) & Linux

---

### F6 — Sneha Kulkarni

sneha.kulkarni@example.invalid · +91 00000 00006 · Nashik

**Summary**
Mechanical engineering graduate seeking an entry-level role.

**Education**
B.E., Mechanical Engineering — Western Plains College of Engineering, 2026 (CGPA 7.2)

**Experience**
*Design Intern* — Ironwheel Fabrication · Jan 2026 – Jun 2026
- Prepared part drawings in AutoCAD and SolidWorks
- Assisted with quality inspection reports in Excel

**Skills**
AutoCAD, SolidWorks, MS Excel, basic C programming

---

### F7 — Arjun Nair

arjun.nair@example.invalid · +91 00000 00007 · Kochi

**Summary**
Computer Science graduate with a year of data analytics internships.

**Education**
B.Tech, Computer Science — Coastal Institute of Technology, 2026 (CGPA 8.3)

**Experience**
*Data Analyst Intern* — Bluefin Analytics · Jul 2025 – Jun 2026
- Analysed sales data in Python with pandas and Jupyter notebooks
- Wrote SQL queries to build weekly reports
- Built dashboards in Power BI

**Skills**
Python, SQL, pandas, NumPy, Power BI, Excel

---

### F8 — Ishaan Verma

ishaan.verma@example.invalid · +91 00000 00008 · Delhi

**Summary**
Motivated Computer Science graduate and fast learner.

**Education**
B.Tech, Computer Science — Capital Institute of Engineering, 2026 (CGPA 7.5)

**Skills**
Python, SQL, REST APIs, Git, Django, FastAPI, pytest, Docker, Kubernetes, AWS, Azure, React, Node.js, Java, Go, Machine Learning, Generative AI, Linux

**Activities**
- Member, college coding club
- Completed an online course on web development

---

### D1 — Priya Sharma

priya.sharma@example.invalid · +91 00000 00011 · Hyderabad

**Summary**
ServiceNow ITSM consultant with 6 years of implementation experience.

**Education**
B.Tech, Computer Science — Deccan Institute of Technology, 2020

**Experience**
*Senior ServiceNow Consultant* — Arcturus Consulting · Jul 2022 – present
- Implemented ServiceNow ITSM for three enterprise clients
- Configured Incident, Problem and Change Management workflows
- Designed Service Catalog items and request fulfilment flows in Flow Designer
- Ran CAB approval configuration and change risk assessment

*ServiceNow Developer* — Orbitwise IT · Jul 2020 – Jun 2022
- Built CMDB discovery integrations and REST integrations with monitoring tools
- Wrote Glide scripts (JavaScript) for business rules and client scripts
- Configured SLA definitions for incident priorities

**Certifications**
ServiceNow Certified System Administrator (CSA); ServiceNow CIS-ITSM; ITIL 4 Foundation

**Skills**
ServiceNow ITSM, Incident Management, Change Management, Problem Management, Service Catalog, CMDB, Flow Designer, JavaScript, SLA Management

---

### D2 — Vikram Joshi

vikram.joshi@example.invalid · +91 00000 00012 · Bengaluru

**Summary**
ServiceNow platform specialist with 5 years supporting IT operations teams.

**Education**
B.Sc., Physics — Garden City College, 2019

**Experience**
*ServiceNow Platform Specialist* — Meridian Global Services · Apr 2021 – present
- Owned ticket handling and outage response workflows on ServiceNow for 4,000 users
- Configured the RFC process, release approvals and the weekly approval board on ServiceNow
- Built request portal items and fulfilment flows for laptops, access and software
- Connected the platform to monitoring alerts through REST

*ServiceNow Administrator* — Pinecrest Solutions · Apr 2020 – Mar 2021
- Maintained forms, lists and user permissions on ServiceNow

**Skills**
ServiceNow, Flow Designer, REST integrations, reporting

---

### D3 — Ananya Reddy

ananya.reddy@example.invalid · +91 00000 00013 · Hyderabad

**Summary**
ServiceNow administrator with 4.5 years of experience.

**Education**
B.Com — Charminar College of Commerce, 2021

**Experience**
*ServiceNow Administrator* — Lotus Infotech · Oct 2022 – present
- Administer the ServiceNow instance for the IT service desk
- Maintain catalogue items, assignment rules and notifications
- Support the change process and prepare changes for approval meetings

*IT Support Analyst* — Lotus Infotech · Apr 2022 – Oct 2022
- Logged and resolved service desk tickets

**Certifications**
ITIL 4 Foundation; ServiceNow Certified System Administrator (CSA)

**Skills**
ServiceNow administration, reporting, user administration

---

### D4 — Rahul Kapoor

rahul.kapoor@example.invalid · +91 00000 00014 · Mumbai

**Summary**
Organisational change management consultant with 8 years leading people through business transformations.

**Education**
MBA, Human Resources — Western Business School, 2017

**Experience**
*Change Management Lead* — Harbourstone Consulting · 2020 – present
- Led change management for an ERP rollout affecting 6,000 employees
- Ran stakeholder analysis, communication plans and training programmes
- Measured adoption and resistance using the Prosci ADKAR model

*HR Business Partner* — Silverline Retail · 2017 – 2020
- Supported restructuring and change communication across four business units

**Certifications**
Prosci Certified Change Practitioner

**Skills**
Change Management, Stakeholder Management, Communication Planning, Training Design

---

### D5 — Sanjana Pillai

sanjana.pillai@example.invalid · +91 00000 00015 · Chennai

**Summary**
ServiceNow developer with 3.5 years of ITSM implementation experience.

**Education**
B.E., Information Technology — Marina Institute of Engineering, 2022

**Experience**
*ServiceNow Developer* — Tidewater Tech · Oct 2023 – present
- Configured Incident Management and Change Management modules, including CAB workflows
- Built Service Catalog items with Flow Designer
- Implemented ServiceNow ITSM for a mid-size client

*Associate ServiceNow Developer* — Tidewater Tech · Apr 2023 – Oct 2023
- Wrote Glide scripts and fixed UI policies

**Certifications**
ServiceNow Certified System Administrator (CSA)

**Skills**
ServiceNow ITSM, Incident Management, Change Management, Service Catalog, Flow Designer, JavaScript

---

### D6 — Karan Malhotra

karan.malhotra@example.invalid · +91 00000 00016 · Gurugram

**Summary**
Backend engineer with 7 years building Java microservices.

**Education**
B.Tech, Computer Science — Northern Institute of Technology, 2019

**Experience**
*Senior Software Engineer* — Crestline Fintech · 2022 – present
- Designed Java Spring Boot microservices and REST APIs for payments
- Led migration to Kubernetes on AWS

*Software Engineer* — Bayview Systems · 2019 – 2022
- Built REST APIs and batch jobs in Java

**Skills**
Java, Spring Boot, REST APIs, Microservices, Kubernetes, AWS, PostgreSQL

---

### D7 — Neha Gupta

neha.gupta@example.invalid · +91 00000 00017 · Noida

**Summary**
ITSM process consultant with 6 years on BMC Remedy and Jira Service Management.

**Education**
B.Tech, Information Technology — Central Plains Institute of Technology, 2020

**Experience**
*ITSM Consultant* — Quillstone Services · 2022 – present
- Configured Incident Management, Problem Management and Change Management in Jira Service Management
- Set up CAB approvals and change calendars
- Designed request types for the employee help portal

*Remedy Administrator* — Fernway Technologies · 2020 – 2022
- Administered BMC Remedy incident and change modules

**Certifications**
ITIL 4 Foundation

**Skills**
Incident Management, Change Management, Problem Management, BMC Remedy, Jira Service Management, SLA Management

---

### D8 — Farhan Qureshi

farhan.qureshi@example.invalid · +91 00000 00018 · Hyderabad

**Summary**
Service desk analyst with 5 years of L1/L2 support experience.

**Education**
B.Sc., Computer Science — Old City Degree College, 2021

**Experience**
*Service Desk Analyst (L2)* — Nexgen Outsourcing · 2023 – present
- Log, prioritise and resolve incidents in ServiceNow
- Escalate tickets to resolver groups and track SLAs

*Service Desk Analyst (L1)* — Nexgen Outsourcing · 2021 – 2023
- Answered user calls and raised tickets in ServiceNow

**Skills**
Incident Management, ServiceNow (end user), customer support, Windows troubleshooting

---

## 5. Calibration settings and expected outcomes

### Calibration settings to use

**JD-F (Junior Python Developer)**
- Core: Python, SQL, REST APIs, Git (4 core → 1 may be missing)
- Supporting: Django/FastAPI, pytest, Linux, Docker
- Preferred: AWS, React
- Minimum experience: 1 year; tolerance: blank (default 1 year)
- Internships count as experience: **100%** (the JD says its 1-year minimum includes internships; 100% is also the default for a fresher JD, minimum ≤ 1 year)
- Education: **Mandatory** (to test the field-family rule)
- Role domain: **Software** (so the software field families apply: IT, ECE, Software Engineering and Computer Applications count as related to Computer Science)

**JD-D (ServiceNow ITSM Consultant)**
- Core: ServiceNow ITSM, Incident Management, Change Management, Service Catalog (4 core → 1 may be missing)
- Change Management meaning: **ITSM**
- Supporting: Problem Management, CMDB, Flow Designer, JavaScript, SLA management
- Preferred: the three certifications
- Minimum experience: 4 years; tolerance: blank (default 1 year)
- Internships count as experience: default (not at all, for a 4-year minimum)
- Education: **Preferred**
- Role domain: **IT operations** (no field families apply while education is preferred)

### Expected outcomes

"Expected" is what a recruiter would want once Phase 3 is done. "Current system risk" is where today's rules will probably differ.

| ID | Tests | Expected | Expected core | Reason | Current system risk |
|---|---|---|---|---|---|
| F1 | Strong fresher; experience from two internships totalling 12 months | PASS | 4/4 | All core skills with evidence; 12 months of internships meets the 1-year minimum, which the JD says includes internships | If internships aren't counted as experience: 0 years, 1 short → HOLD |
| F2 | Skills shown only in projects; no internship | HOLD | 4/4 | Core skills clearly shown in projects (Flask REST API, PostgreSQL SQL, GitHub), but 0 years against a 1-year minimum, within tolerance → near-miss | Project-only skills not detected (no skills section) → core fails → REJECT |
| F3 | IT degree for a "Computer Science" JD | PASS | 4/4 | IT is a related field; 12 months of internship meets the minimum | Mandatory CS education rejects the IT degree → REJECT |
| F4 | ECE degree; 6-month internship | HOLD | 4/4 | ECE treated as related for software roles; 0.5 years short (within tolerance) → one near-miss | Education rejects ECE, plus experience short → REJECT |
| F5 | British spelling and compound skill phrases ("Python & SQL", "RESTful API Design & Development", "Version Control (Git)") | PASS | 4/4 | Splitting and spelling normalisation should find all four core skills; 12 months of internship | Compound phrases or "RESTful API" not mapped to REST APIs → fewer core matches → HOLD |
| F6 | Clearly weak fit (mechanical engineer) | REJECT | 0/4 | No core skills; unrelated degree | Low risk |
| F7 | Data analyst: missing REST APIs and Git | HOLD | 2/4 | 2 missing with 1 allowed → one near-miss; experience met | Low risk |
| F8 | Keyword-stuffed skills list, no evidence, no experience | HOLD | 4/4 (listed only) | Skills only listed, never shown in use; 0 years (within tolerance) → near-miss; a person should check | Skills-section entries auto-confirm, so the system may treat the core as fully proven; outcome still HOLD on experience |
| D1 | Strong domain candidate, exact terms, certifications | PASS | 4/4 | Everything matches with evidence | Low risk |
| D2 | Domain experience in paraphrase ("ticket handling and outage response", "RFC process, release approvals", "request portal items") | PASS | 4/4 | A recruiter would read these as incident, change and catalogue work on ServiceNow | Paraphrases not matched → core 1/4 → REJECT; functional score (word overlap) also misses them |
| D3 | Skills partly shown through certifications (ITIL 4, CSA) and admin duties | PASS | 3/4 + 1 uncertain (incident) | ServiceNow ITSM from the CSA and admin of the ServiceNow instance; change and catalogue work shown ("support the change process", "maintain catalogue items"); incident management only implied by ITIL 4 and service-desk work, so uncertain - one uncertain core skill is within the allowance of one missing; 4.5 years (as of 2026-10-14) meets the minimum | Without certification mapping, incident not found; could be HOLD or REJECT depending on matches |
| D4 | Organisational change management, no ServiceNow | REJECT | 0/4 | Change Management here is the organisational sense; JD needs the ITSM sense | If senses aren't applied, Change Management matches (1/4); still REJECT |
| D5 | Strong ITSM, 3.5 years against a 4-year minimum | HOLD | 4/4 | 0.5 years short (3.5 years as of 2026-10-14), within tolerance → near-miss | Low risk |
| D6 | Strong generalist, no domain experience | REJECT | 0/4 | No ITSM skills | Low risk |
| D7 | ITSM processes on another platform (Remedy, Jira SM) | HOLD | 2/4 | Incident and change (ITSM) present; ServiceNow ITSM and Service Catalog missing ("request types" isn't the catalogue) → one near-miss | "Request types" may be matched to Service Catalog → 3/4 → PASS |
| D8 | ServiceNow end user, not implementer | REJECT | 1/4 (incident) | Logs tickets in ServiceNow but doesn't configure it; change and catalogue missing; ServiceNow ITSM as an implementation skill not shown | "ServiceNow" in the text may be confirmed as ServiceNow ITSM → 2/4 → HOLD |

### expectations.json shape (example entry)

```json
{
  "id": "F3",
  "jd": "JD-F",
  "tests": ["education_field_family"],
  "expected_outcome": "PASS",
  "expected_core": "4/4",
  "reason": "IT is a related field; 12 months of internship meets the minimum",
  "current_system_risk": "Mandatory CS education rejects the IT degree -> REJECT"
}
```
