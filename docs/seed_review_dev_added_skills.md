# Review: dev-added skills before export

**Reviewed 2026-10-26.** Every decision is applied in dev (audited as the reviewing HR admin), the 26 synthetic candidates re-scored with no change (Phase 3 16/16, H1-H10 unchanged), and the seed regenerated with these sections approved. The 4 skills added after that review were reviewed on 2026-10-30 (see the last section). Nothing is left for review.

Production checklist item (see [roadmap](roadmap.md)). These 104 skills are in `app/seeds/product_config/catalogue_dev_added.json`, which does not load until reviewed. To approve: fix, merge or drop rows in that file (or in dev, then regenerate it with `scripts/config_seed/`), then set `approved: true` on its sections. (Done - regenerated with `--reviewed 2026-10-26`.)

**Nothing personal found:** no person's name, email or phone among the names, aliases or descriptions. Status: Done 25, Keep 79.

The rules in the same file wait for these skills: the 2 Change Management senses, the 2 pattern rules (AI Governance, Program Management) and all 11 certification mappings.

| Skill | Origin | Used by | Status | Note |
|---|---|---|---|---|
| apache | Added by hand, 2026-08-31 | - | Done | Dropped (decided 2026-10-26). (Inactive - not seeded.) |
| APIs | Added by hand, 2026-09-23 | - | Done | Dropped (decided 2026-10-26). (Inactive - not seeded.) |
| AWS ALB | Added by hand, 2026-08-31 | - | Done | Was 'alb' under Blockchain - renamed AWS ALB, category Cloud. |
| catalog federation | Added by hand, 2026-08-31 | - | Done | Dropped (decided 2026-10-26). (Inactive - not seeded.) |
| Crew AI | Added by hand, 2026-08-31 | - | Done | Merged into CrewAI (now its alias); dropped. (Inactive - not seeded.) |
| CrewAI Enterprise | Added by hand, 2026-08-31 | - | Done | Merged into CrewAI with its aliases; dropped. (Inactive - not seeded.) |
| CrewAI Framework | Added by hand, 2026-08-31 | - | Done | Merged into CrewAI (now its alias); dropped. (Inactive - not seeded.) |
| Databricks Asset Bundles | Added by hand, 2026-08-31 | - | Done | Was 'dab deployments' - renamed, category Data Engineering. |
| databricks notebooks | Added by hand, 2026-08-31 | - | Done | Merged into Databricks (now its alias); dropped. (Inactive - not seeded.) |
| delta lake with python | Added by hand, 2026-08-31 | - | Done | Dropped - Delta Lake exists. (Inactive - not seeded.) |
| delta lake with sparks sql in databricks | Added by hand, 2026-08-31 | - | Done | Dropped - Delta Lake and Spark SQL exist. (Inactive - not seeded.) |
| Exception Handling | Added by hand, 2026-08-31 | - | Done | Kept (decided 2026-10-26). |
| File Handling | Added by hand, 2026-08-31 | - | Done | Kept (decided 2026-10-26). |
| HTTP methods | Added by hand, 2026-09-17 | - | Done | Kept, as a child of REST APIs. |
| Lakeflow | Added by hand, 2026-08-31 | - | Done | Was 'lake flow' - renamed, category Data Engineering. |
| mysql rds | Added by hand, 2026-08-31 | - | Done | Merged into RDS (Amazon RDS) as an alias; dropped. (Inactive - not seeded.) |
| Object-Oriented Programming | Added by hand, 2026-08-31 | - | Done | Self-alias removed. |
| pip | Added by hand, 2026-08-31 | - | Done | Dropped (decided 2026-10-26). (Inactive - not seeded.) |
| prompt engineering | Added by hand, 2026-08-31 | - | Done | Self-alias removed. |
| REST | Added by hand, 2026-09-23 | - | Done | Merged into REST APIs (now its alias); dropped. (Inactive - not seeded.) |
| semantic analysis | Added by hand, 2026-08-31 | - | Done | Wrong LangGraph aliases and LangChain parent removed. |
| ServiceNow development | Added by hand, 2026-09-23 | 1 [P3] JD | Done | Alias 'ServiceNow' removed. |
| tranformers | Added by hand, 2026-08-31 | - | Done | Typo; 'transformers' is already an alias of Hugging Face Transformers, so it was cleaned and dropped (merged there) rather than renamed - a rename would collide with that alias. (Inactive - not seeded.) |
| Version Control | Added by hand, 2026-08-31 | parent of: git | Done | Kept, as the parent of git. |
| VS Code | Added by hand, 2026-09-23 | - | Done | Kept (decided 2026-10-26). |
| AI Governance | Promoted from an unknown skill, 2026-09-30 | pattern rules: AI near governance (Experience) | Keep | Created the day the real campaign 'AI transformation lead Sep' was set up (now deleted) - a generic term, nothing personal. |
| AI Operating Models | Promoted from an unknown skill, 2026-09-30 | - | Keep | Created the day the real campaign 'AI transformation lead Sep' was set up (now deleted) - a generic term, nothing personal. |
| AI Transformation | Promoted from an unknown skill, 2026-09-30 | - | Keep | Created the day the real campaign 'AI transformation lead Sep' was set up (now deleted) - a generic term, nothing personal. |
| algorithms | Added by hand, 2026-09-17 | - | Keep | Kept as is (decided 2026-10-26); 'data structures' and 'algorithms' were not merged into it. |
| amazon s3 | Added by hand, 2026-08-31 | - | Keep |  |
| Application Portfolio Management (APM) | Added by hand, 2026-09-23 | - | Keep |  |
| AutoGen Studio | Added by hand, 2026-08-31 | - | Keep |  |
| aws bedrock | Added by hand, 2026-08-31 | - | Keep |  |
| aws ses | Added by hand, 2026-08-31 | - | Keep |  |
| azure ai | Added by hand, 2026-08-31 | - | Keep |  |
| azure databricks | Added by hand, 2026-08-31 | - | Keep |  |
| Business Rules | Added by hand, 2026-09-23 | - | Keep |  |
| Change Management | Promoted from an unknown skill, 2026-09-30 | senses: ORGANISATIONAL, ITSM; certifications: ITIL 4 Foundation; ITIL Foundation (v3 / unversioned); ServiceNow CIS-ITSM; 1 [P3] JD | Keep | Created the day the real campaign 'AI transformation lead Sep' was set up (now deleted) - a generic term, nothing personal. |
| Client Scripts | Added by hand, 2026-09-23 | - | Keep |  |
| Cloud Architecture | Added by hand, 2026-08-31 | - | Keep | Renamed from 'cloud architecture' 2026-10-26. |
| CMDB | Promoted from an unknown skill, 2026-10-14 | 1 [P3] JD | Keep |  |
| CSDM | Added by hand, 2026-09-23 | - | Keep |  |
| data factory | Added by hand, 2026-08-31 | - | Keep |  |
| Data Modelling | Added by hand, 2026-09-23 | - | Keep |  |
| data pipeline design | Added by hand, 2026-08-31 | - | Keep |  |
| data structures | Added by hand, 2026-09-17 | - | Keep | Kept as is (decided 2026-10-26); 'data structures' and 'algorithms' were not merged into it. |
| Data Structures and Algorithms | Added by hand, 2026-08-31 | - | Keep | Kept as is (decided 2026-10-26); 'data structures' and 'algorithms' were not merged into it. |
| data warehousing | Added by hand, 2026-08-31 | - | Keep |  |
| Demand Management | Added by hand, 2026-09-23 | - | Keep |  |
| Digital Transformation | Promoted from an unknown skill, 2026-09-30 | - | Keep | Created the day the real campaign 'AI transformation lead Sep' was set up (now deleted) - a generic term, nothing personal. |
| Django ORM | Added by hand, 2026-09-17 | - | Keep |  |
| Enterprise Architecture (EA) | Added by hand, 2026-09-23 | - | Keep |  |
| etl processes | Added by hand, 2026-08-31 | - | Keep |  |
| Executive Reporting | Promoted from an unknown skill, 2026-09-30 | - | Keep | Created the day the real campaign 'AI transformation lead Sep' was set up (now deleted) - a generic term, nothing personal. |
| faiss | Added by hand, 2026-08-31 | - | Keep |  |
| Financial Planning | Added by hand, 2026-09-23 | - | Keep |  |
| Flow Designer | Added by hand, 2026-09-23 | 1 [P3] JD | Keep |  |
| git | Added by hand, 2026-08-31 | pattern rules: GitHub / GitLab / Bitbucket imply Git; 1 [P3] JD | Keep |  |
| Glide APIs | Added by hand, 2026-09-23 | - | Keep |  |
| Import Sets | Added by hand, 2026-09-23 | - | Keep |  |
| Incident Management | Added in JD calibration (HR), 2026-10-14 | certifications: ITIL 4 Foundation; ITIL Foundation (v3 / unversioned); ServiceNow CIS-ITSM; 1 [P3] JD | Keep |  |
| IntegrationHub | Added by hand, 2026-09-23 | - | Keep |  |
| Investment Funding | Added by hand, 2026-09-23 | - | Keep |  |
| ITBM | Added by hand, 2026-09-23 | - | Keep |  |
| jpa | Added by hand, 2026-08-31 | - | Keep |  |
| jwt | Added by hand, 2026-08-31 | - | Keep |  |
| k3s | Added by hand, 2026-08-31 | - | Keep |  |
| large language models | Added by hand, 2026-08-31 | - | Keep |  |
| Linux | Added by hand, 2026-08-31 | 1 [P3] JD | Keep |  |
| nginx | Added by hand, 2026-08-31 | - | Keep |  |
| performance tuning | Added by hand, 2026-08-31 | - | Keep |  |
| Portfolio Hierarchy | Added by hand, 2026-09-23 | - | Keep |  |
| Portfolio Planning | Added by hand, 2026-09-23 | - | Keep |  |
| Problem Management | Promoted from an unknown skill, 2026-10-14 | certifications: ITIL 4 Foundation; ITIL Foundation (v3 / unversioned); 1 [P3] JD | Keep |  |
| Program Management | Promoted from an unknown skill, 2026-09-30 | pattern rules: Program Head/Manager/Director (job title); certifications: PMP | Keep | Created the day the real campaign 'AI transformation lead Sep' was set up (now deleted) - a generic term, nothing personal. |
| Project Portfolio Management (PPM) | Added by hand, 2026-09-23 | - | Keep |  |
| RAG | Added by hand, 2026-08-31 | - | Keep |  |
| rancher | Added by hand, 2026-08-31 | - | Keep |  |
| relational databases | Added by hand, 2026-09-17 | - | Keep |  |
| Resource Management | Added by hand, 2026-09-23 | - | Keep |  |
| Responsible AI | Promoted from an unknown skill, 2026-09-30 | - | Keep | Created the day the real campaign 'AI transformation lead Sep' was set up (now deleted) - a generic term, nothing personal. |
| REST APIs | Added by hand, 2026-08-31 | parent of: HTTP methods; 1 [P3] JD | Keep |  |
| Risk Management | Promoted from an unknown skill, 2026-09-30 | - | Keep | Created the day the real campaign 'AI transformation lead Sep' was set up (now deleted) - a generic term, nothing personal. |
| Script Includes | Added by hand, 2026-09-23 | - | Keep |  |
| sendgrid | Added by hand, 2026-08-31 | - | Keep |  |
| Service Catalog | Promoted from an unknown skill, 2026-10-14 | 1 [P3] JD | Keep |  |
| ServiceNow integrations | Added by hand, 2026-09-23 | - | Keep |  |
| ServiceNow ITSM | Added in JD calibration (HR), 2026-10-14 | certifications: ServiceNow CSA; ServiceNow CIS-ITSM; 1 [P3] JD | Keep |  |
| ServiceNow SPM | Added by hand, 2026-09-23 | - | Keep |  |
| shedlock | Added by hand, 2026-08-31 | - | Keep |  |
| SLA management | Promoted from an unknown skill, 2026-10-14 | 1 [P3] JD | Keep |  |
| SOAP APIs | Added by hand, 2026-09-23 | - | Keep |  |
| spring mvc | Added by hand, 2026-08-31 | - | Keep |  |
| sql | Added by hand, 2026-08-31 | parent of: PostgreSQL, MySQL; 1 [P3] JD | Keep |  |
| Strategic Planning Workspace | Added by hand, 2026-09-23 | - | Keep |  |
| Technology Governance | Promoted from an unknown skill, 2026-09-30 | - | Keep | Created the day the real campaign 'AI transformation lead Sep' was set up (now deleted) - a generic term, nothing personal. |
| Transform Maps | Added by hand, 2026-09-23 | - | Keep |  |
| UI Actions | Added by hand, 2026-09-23 | - | Keep |  |
| UI Builder | Added by hand, 2026-09-23 | - | Keep |  |
| UI Policies | Added by hand, 2026-09-23 | - | Keep |  |
| Unit Testing | Added by hand, 2026-08-31 | parent of: pytest | Keep |  |
| unity catalog | Added by hand, 2026-08-31 | - | Keep |  |
| vector databases | Added by hand, 2026-08-31 | - | Keep |  |
| Virtual Environments | Added by hand, 2026-08-31 | - | Keep |  |

## Added after the review - reviewed 2026-10-30

Added in dev on 2026-10-26 while calibrating the senior test JD ("Head of Data & AI Platforms", tests/fixtures/senior/).
Nothing personal. Descriptions added 2026-10-30 (audited, as the reviewing HR admin).

**Reviewed 2026-10-30, all kept as they are:**
- Data Governance stays under Data Engineering (there is no "Data Management" category).
- Stakeholder Management takes the category Program Management uses. Program Management has none (no active skill
  has a business or management category), so it stays without one.
- AWS Certified Solutions Architect stays a preferred skill.

They're approved with the rest of `app/seeds/product_config/catalogue_dev_added.json`, regenerated with
`--reviewed 2026-10-30 --approve-pending`.

| Skill | Origin | Used by | Category | Aliases | Description | Note |
|---|---|---|---|---|---|---|
| Data Engineering | Unknown skill from the senior JD, added as a new catalogue skill in calibration | Senior JD: core, required | Data Engineering | - | Building and running data pipelines and platforms: batch and streaming ingestion, ETL/ELT, data lakes, lakehouses and warehouses. | Read by the paraphrase search and the voters. Senior dry run with it (2026-10-30): no outcome change; S4 gains Data Engineering from "Built data warehouse reports and ETL jobs in SAP BW" (0.453, still REJECT). |
| Data Governance | Unknown skill from the senior JD, added as a new catalogue skill in calibration | Senior JD: supporting | Data Engineering | - | Governing an organisation's data: data quality, ownership and stewardship, cataloguing, lineage, access policies and compliance. | Category is Data Engineering, as set when it was approved in calibration. |
| Stakeholder Management | Unknown skill from the senior JD, added as a new catalogue skill in calibration | Senior JD: supporting | - | - | Managing the people a project or service affects: identifying stakeholders, setting expectations, reporting and keeping them aligned. | No category. |
| AWS Certified Solutions Architect | Extracted as a certification; added as a skill in calibration | Senior JD: preferred | - | - | - | A certification held as a preferred skill, by decision (no certification mapping). No description. |

**Changed after the review, in an approved section:** REST APIs (reviewed: Keep) gained a "Not this skill" text on
2026-10-30, as the reviewing HR admin: "only testing or calling APIs, without designing or building them". It loads
with the reviewed section (accepted 2026-10-30).

