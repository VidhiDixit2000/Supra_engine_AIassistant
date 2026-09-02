-- ============================================================
-- Supra Hospital Assistant — schema migration
-- Run in the Supabase SQL editor. Safe to re-run.
-- ============================================================

-- ------------------------------------------------------------
-- 1. Knowledge table: governance metadata
-- ------------------------------------------------------------

alter table hospital_knowledge
  add column if not exists safety_critical boolean default false,
  add column if not exists patient          text,
  add column if not exists owner            text,
  add column if not exists banned_drugs     text[];

-- Safety-critical records cross department walls.
-- These are the ones a doctor from ANY department must see.
update hospital_knowledge set safety_critical = true
where title in (
  'Post-TKR Pain Management',
  'Patient Rajan Drug Alert',
  'Sepsis Protocol v3',
  'DVT Prophylaxis',
  'Diabetic Fasting Protocol',
  'TKR Discharge Rule',
  'Warfarin-NSAID Interaction',
  'Verbal Orders Policy',
  'Padma Fasting DM'
);

-- Patient pinning. Lowercase single token — the backend
-- word-boundary matches this against the question.
update hospital_knowledge set patient = 'rajan'
where title = 'Patient Rajan Drug Alert';

update hospital_knowledge set patient = 'padma'
where title = 'Padma Fasting DM';

-- Explicit prohibition list. Without this the backend falls
-- back to parsing the record text, which works for the
-- supplied records but is not something to rely on.
update hospital_knowledge
set banned_drugs = array['ibuprofen','aspirin','diclofenac']
where title = 'Patient Rajan Drug Alert';

-- Provenance. Shown to the doctor so an instruction can be
-- verified or challenged rather than merely trusted.
update hospital_knowledge set owner = 'Dr. Vikram, January 2025'
where title = 'Post-TKR Pain Management';

update hospital_knowledge set owner = 'Critical Care Committee, 2026 (supersedes v2)'
where title = 'Sepsis Protocol v3';

update hospital_knowledge set owner = 'Pharmacy & Therapeutics'
where title in ('Warfarin-NSAID Interaction', 'Formulary Brands');

update hospital_knowledge set owner = 'Hospital Admin (post-incident 2023)'
where title = 'Verbal Orders Policy';


-- ------------------------------------------------------------
-- 2. Users: scope of practice
-- ------------------------------------------------------------

alter table hospital_users
  add column if not exists can_prescribe boolean;

update hospital_users set can_prescribe = true
where role ilike '%doctor%' or role ilike '%hod%';

update hospital_users set can_prescribe = false
where role ilike '%nurse%' or role ilike '%admin%';


-- ------------------------------------------------------------
-- 3. Audit trail
--
-- The NABH / DPDP argument in the design document is
-- theoretical without this table and demonstrable with it.
-- ------------------------------------------------------------

create table if not exists query_audit (
  id               bigserial primary key,
  created_at       timestamptz default now(),
  user_id          text,
  user_name        text,
  user_role        text,
  department       text,
  question         text,
  records_used     text[],
  records_withheld int,
  guardrails_fired text[],
  answer_preview   text
);

create index if not exists query_audit_created_idx
  on query_audit (created_at desc);


-- ------------------------------------------------------------
-- 4. Verify department strings match between the two tables
--    before demoing. A mismatch here fails silently — every
--    ortho query would return global records only.
-- ------------------------------------------------------------

-- select distinct department from hospital_users;
-- select distinct department from hospital_knowledge;